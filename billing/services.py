from django.db import transaction
from django.db.models import F, Max

from .exceptions import InsufficientStock
from .models import Invoice, InvoiceItem, Payment, Product
from .tasks import send_invoice_email

VAT_RATE = 0.13


def next_invoice_number(business):
    last = Invoice.objects.filter(business=business).aggregate(n=Max("number"))["n"]
    return (last or 0) + 1


def calculate_totals(invoice):
    subtotal = 0
    for item in invoice.items.all():
        subtotal += float(item.unit_price) * item.quantity
    tax = round(subtotal * VAT_RATE, 2)
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

    for row in items:
        product = Product.objects.get(pk=row["product_id"], business=business)
        quantity = row["quantity"]
        if product.stock < quantity:
            raise InsufficientStock(product.sku, product.stock, quantity)

        product.stock -= quantity
        product.save(update_fields=["stock"])

        InvoiceItem.objects.create(
            invoice=invoice,
            product=product,
            quantity=quantity,
            unit_price=product.unit_price,
            line_total=float(product.unit_price) * quantity,
        )

    calculate_totals(invoice)
    invoice.save(update_fields=["subtotal", "tax", "total"])

    send_invoice_email.delay(invoice.id)
    return invoice


@transaction.atomic
def record_payment(*, invoice, amount, method, user=None, gateway_ref=""):
    payment = Payment.objects.create(
        invoice=invoice,
        amount=amount,
        method=method,
        gateway_ref=gateway_ref,
        recorded_by=user,
    )

    invoice.amount_paid += amount
    if invoice.amount_paid >= invoice.total:
        invoice.status = Invoice.Status.PAID
    else:
        invoice.status = Invoice.Status.PARTIALLY_PAID
    invoice.save(update_fields=["amount_paid", "status"])
    return payment


@transaction.atomic
def cancel_invoice(invoice):
    for item in invoice.items.select_related("product"):
        Product.objects.filter(pk=item.product_id).update(stock=F("stock") + item.quantity)
    invoice.status = Invoice.Status.CANCELLED
    invoice.save(update_fields=["status"])
    return invoice
