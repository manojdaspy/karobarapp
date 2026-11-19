import datetime
import logging
import zoneinfo
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import F, Max, Sum
from django.utils import timezone

from .exceptions import (
    InsufficientStock,
    InvalidPaymentAmount,
    InvoiceCancellationNotAllowed,
    ReturnNotAllowed,
    ReturnQuantityExceeded,
)
from .models import (
    CreditNote,
    CreditNoteItem,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentReminderLog,
    Product,
)
from .tasks import send_invoice_email, send_sms

logger = logging.getLogger(__name__)

VAT_RATE = Decimal("0.13")


def quantize_money(amount):
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def next_invoice_number(business):
    last = Invoice.objects.filter(business=business).aggregate(n=Max("number"))["n"]
    return (last or 0) + 1


def next_credit_note_number(business):
    last = CreditNote.objects.filter(business=business).aggregate(n=Max("number"))["n"]
    return (last or 0) + 1


def calculate_totals(invoice):
    subtotal = Decimal("0.00")
    for item in invoice.items.all():
        subtotal += item.unit_price * item.quantity
    tax = quantize_money(subtotal * VAT_RATE)
    invoice.subtotal = subtotal
    invoice.tax = tax
    invoice.total = subtotal + tax


@transaction.atomic
def create_invoice(*, business, customer, due_date, items, user):
    invoice = Invoice.objects.create(
        business=business,
        customer=customer,
        number=next_invoice_number(business),
        status=Invoice.Status.ISSUED,
        due_date=due_date,
        created_by=user,
    )

    product_ids = sorted([row["product_id"] for row in items])
    locked_products = {
        p.id: p
        for p in Product.objects.select_for_update().filter(pk__in=product_ids, business=business)
    }

    for row in items:
        product = locked_products.get(row["product_id"])
        if not product:
            raise Product.DoesNotExist(f"Product {row['product_id']} not found.")

        quantity = row["quantity"]
        if product.stock < quantity:
            raise InsufficientStock(product.sku, product.stock, quantity)

        product.stock -= quantity
        product.save(update_fields=["stock"])

        line_total = quantize_money(product.unit_price * quantity)
        InvoiceItem.objects.create(
            invoice=invoice,
            product=product,
            quantity=quantity,
            unit_price=product.unit_price,
            line_total=line_total,
        )

    calculate_totals(invoice)
    invoice.save(update_fields=["subtotal", "tax", "total"])

    transaction.on_commit(lambda: send_invoice_email.delay(invoice.id))
    return invoice


@transaction.atomic
def record_payment(*, invoice, amount, method, user=None, gateway_ref=""):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)

    if invoice.status == Invoice.Status.CANCELLED:
        raise InvalidPaymentAmount("Cannot record payment for a cancelled invoice.")

    amount = quantize_money(amount)
    if amount <= Decimal("0.00"):
        raise InvalidPaymentAmount("Payment amount must be greater than zero.")

    if amount > invoice.balance:
        raise InvalidPaymentAmount(
            f"Payment amount (Rs. {amount}) exceeds outstanding invoice balance (Rs. {invoice.balance})."
        )

    payment = Payment.objects.create(
        invoice=invoice,
        amount=amount,
        method=method,
        gateway_ref=gateway_ref,
        recorded_by=user,
    )

    invoice.amount_paid += amount
    if invoice.balance <= Decimal("0.00"):
        invoice.status = Invoice.Status.PAID
    else:
        invoice.status = Invoice.Status.PARTIALLY_PAID
    invoice.save(update_fields=["amount_paid", "status"])
    return payment


@transaction.atomic
def cancel_invoice(invoice):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == Invoice.Status.CANCELLED:
        raise InvoiceCancellationNotAllowed("Invoice is already cancelled.")
    if invoice.status in (Invoice.Status.PAID, Invoice.Status.PARTIALLY_PAID) or invoice.amount_paid > Decimal("0.00"):
        raise InvoiceCancellationNotAllowed("Invoices with payments cannot be cancelled.")
    if invoice.amount_credited > Decimal("0.00"):
        raise InvoiceCancellationNotAllowed("Invoices with credit notes cannot be cancelled.")

    for item in invoice.items.select_related("product"):
        Product.objects.filter(pk=item.product_id).update(stock=F("stock") + item.quantity)

    invoice.status = Invoice.Status.CANCELLED
    invoice.save(update_fields=["status"])
    return invoice


@transaction.atomic
def process_return(*, invoice, items, reason="", refund_method=CreditNote.RefundMethod.INVOICE_OFFSET, user=None):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)

    if invoice.status not in (Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.PAID):
        raise ReturnNotAllowed(f"Cannot process return for an invoice with status '{invoice.status}'.")

    item_map = {row["invoice_item_id"]: row["quantity"] for row in items}
    if len(item_map) != len(items):
        raise ReturnNotAllowed("Duplicate invoice items in return request.")

    invoice_items = {
        item.id: item
        for item in InvoiceItem.objects.select_for_update()
        .filter(pk__in=item_map.keys(), invoice=invoice)
        .select_related("product")
    }

    if len(invoice_items) != len(item_map):
        raise ReturnNotAllowed("One or more items do not belong to this invoice.")

    # Validate returnable quantities and compute totals
    subtotal = Decimal("0.00")
    validated_lines = []

    for item_id, requested_qty in item_map.items():
        if requested_qty <= 0:
            raise ReturnNotAllowed("Return quantity must be greater than zero.")

        inv_item = invoice_items[item_id]
        already_returned = (
            CreditNoteItem.objects.filter(invoice_item=inv_item).aggregate(s=Sum("quantity"))["s"]
            or 0
        )
        returnable = inv_item.quantity - already_returned
        if requested_qty > returnable:
            raise ReturnQuantityExceeded(inv_item.product.sku, returnable, requested_qty)

        line_total = quantize_money(inv_item.unit_price * requested_qty)
        subtotal += line_total
        validated_lines.append((inv_item, requested_qty, line_total))

    tax = quantize_money(subtotal * VAT_RATE)
    credit_total = subtotal + tax

    # Financial split: offset invoice balance first, excess becomes refund
    current_balance = invoice.balance
    applied_amount = min(credit_total, max(Decimal("0.00"), current_balance))
    refund_amount = credit_total - applied_amount

    # Auto-adjust refund method if refund is owed but method was invoice_offset
    if refund_amount > Decimal("0.00") and refund_method == CreditNote.RefundMethod.INVOICE_OFFSET:
        refund_method = CreditNote.RefundMethod.CASH

    credit_note = CreditNote.objects.create(
        business=invoice.business,
        invoice=invoice,
        customer=invoice.customer,
        number=next_credit_note_number(invoice.business),
        issue_date=timezone.localdate(),
        reason=reason,
        subtotal=subtotal,
        tax=tax,
        total=credit_total,
        applied_amount=applied_amount,
        refund_amount=refund_amount,
        refund_method=refund_method,
        created_by=user,
    )

    for inv_item, requested_qty, line_total in validated_lines:
        CreditNoteItem.objects.create(
            credit_note=credit_note,
            invoice_item=inv_item,
            product=inv_item.product,
            quantity=requested_qty,
            unit_price=inv_item.unit_price,
            line_total=line_total,
        )
        # Restock product inventory atomically
        Product.objects.filter(pk=inv_item.product_id).update(stock=F("stock") + requested_qty)

    # Update invoice credited amount and status
    invoice.amount_credited += applied_amount
    if invoice.balance <= Decimal("0.00"):
        invoice.status = Invoice.Status.PAID
    invoice.save(update_fields=["amount_credited", "status"])

    return credit_note


def is_quiet_hours(now=None):
    tz = zoneinfo.ZoneInfo(settings.TIME_ZONE)
    local_now = (now or timezone.now()).astimezone(tz)
    start = getattr(settings, "REMINDER_QUIET_HOURS_START", 20)
    end = getattr(settings, "REMINDER_QUIET_HOURS_END", 8)
    if start > end:
        return local_now.hour >= start or local_now.hour < end
    return start <= local_now.hour < end


def send_invoice_reminder(invoice):
    """
    Evaluates eligibility, quiet hours, cooldown, and send limits,
    then dispatches an SMS reminder outside the database transaction.
    """
    with transaction.atomic():
        invoice = (
            Invoice.objects.select_for_update()
            .select_related("customer", "business")
            .get(pk=invoice.pk)
        )

        today = timezone.localdate()
        if (
            invoice.status not in (Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID)
            or invoice.balance <= Decimal("0.00")
            or invoice.due_date >= today
            or not invoice.customer.phone
        ):
            PaymentReminderLog.objects.create(
                business=invoice.business,
                invoice=invoice,
                customer=invoice.customer,
                phone=invoice.customer.phone or "",
                message="",
                status=PaymentReminderLog.Status.SKIPPED_NOT_ELIGIBLE,
            )
            return False, "Invoice is not eligible for a reminder."

        if is_quiet_hours():
            PaymentReminderLog.objects.create(
                business=invoice.business,
                invoice=invoice,
                customer=invoice.customer,
                phone=invoice.customer.phone,
                message="",
                status=PaymentReminderLog.Status.SKIPPED_QUIET_HOURS,
            )
            return False, "Quiet hours active (20:00 to 08:00). Reminder skipped."

        successful_logs = PaymentReminderLog.objects.filter(
            invoice=invoice, status=PaymentReminderLog.Status.SENT
        )

        max_attempts = getattr(settings, "REMINDER_MAX_ATTEMPTS", 3)
        if successful_logs.count() >= max_attempts:
            PaymentReminderLog.objects.create(
                business=invoice.business,
                invoice=invoice,
                customer=invoice.customer,
                phone=invoice.customer.phone,
                message="",
                status=PaymentReminderLog.Status.SKIPPED_LIMIT,
            )
            return False, f"Maximum of {max_attempts} reminders already sent."

        cooldown_hours = getattr(settings, "REMINDER_COOLDOWN_HOURS", 24)
        cutoff = timezone.now() - datetime.timedelta(hours=cooldown_hours)
        if successful_logs.filter(sent_at__gte=cutoff).exists():
            PaymentReminderLog.objects.create(
                business=invoice.business,
                invoice=invoice,
                customer=invoice.customer,
                phone=invoice.customer.phone,
                message="",
                status=PaymentReminderLog.Status.SKIPPED_COOLDOWN,
            )
            return False, f"Reminder cooldown active ({cooldown_hours} hours)."

        text = (
            f"Dear {invoice.customer.name}, your invoice #{invoice.number} from "
            f"{invoice.business.name} of Rs. {invoice.balance} was due on "
            f"{invoice.due_date:%d %b %Y}. Please clear your dues. Thank you."
        )

        log_entry = PaymentReminderLog.objects.create(
            business=invoice.business,
            invoice=invoice,
            customer=invoice.customer,
            phone=invoice.customer.phone,
            message=text,
            status=PaymentReminderLog.Status.SENT,
        )

    # Network dispatch outside transaction to avoid holding DB row lock
    try:
        send_sms(invoice.customer.phone, text)
    except Exception as exc:
        logger.warning("Failed to send reminder SMS for invoice %s: %s", invoice.id, exc)
        log_entry.status = PaymentReminderLog.Status.FAILED
        log_entry.save(update_fields=["status"])
        return False, f"SMS dispatch failed: {exc}"

    return True, "Reminder sent successfully."
