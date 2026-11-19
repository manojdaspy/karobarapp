import logging
from decimal import Decimal

import requests
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def send_invoice_email(invoice_id):
    from .models import Invoice

    invoice = Invoice.objects.select_related("customer", "business").get(pk=invoice_id)
    if not invoice.customer.email:
        return

    send_mail(
        subject=f"Invoice #{invoice.number} from {invoice.business.name}",
        message=(
            f"Dear {invoice.customer.name},\n\n"
            f"Your invoice #{invoice.number} for Rs. {invoice.total} is due on "
            f"{invoice.due_date:%d %b %Y}.\n\nThank you for your business."
        ),
        from_email=None,
        recipient_list=[invoice.customer.email],
    )


def send_sms(phone, text):
    response = requests.post(
        settings.SMS_GATEWAY_URL,
        json={"to": phone, "text": text},
        headers={"Authorization": f"Bearer {settings.SMS_GATEWAY_TOKEN}"},
        timeout=10,
    )
    response.raise_for_status()


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def process_gateway_payment(self, payload):
    from .models import Invoice, Payment
    from .services import record_payment

    transaction_id = payload.get("transaction_id", "")
    if transaction_id and Payment.objects.filter(gateway_ref=transaction_id).exists():
        logger.info("Gateway transaction %s already processed. Skipping duplicate.", transaction_id)
        return

    invoice = Invoice.objects.select_related("customer").get(pk=payload["invoice_id"])
    amount = Decimal(str(payload["amount"]))

    try:
        record_payment(
            invoice=invoice,
            amount=amount,
            method=Payment.Method.GATEWAY,
            gateway_ref=transaction_id,
        )
    except IntegrityError:
        logger.info("Duplicate gateway transaction %s caught by DB constraint.", transaction_id)
        return

    logger.info("Recorded gateway payment %s for invoice %s", transaction_id, invoice.pk)

    if invoice.customer.phone:
        try:
            send_sms(
                invoice.customer.phone,
                f"Payment of Rs. {amount} received for invoice #{invoice.number}. Thank you!",
            )
        except Exception as exc:
            logger.warning(
                "Payment recorded for invoice %s, but receipt SMS failed: %s", invoice.pk, exc
            )


@shared_task
def send_overdue_payment_reminders():
    from .models import Invoice
    from .services import send_invoice_reminder

    today = timezone.localdate()
    overdue_invoices = Invoice.objects.filter(
        due_date__lt=today,
        status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID],
    ).select_related("customer", "business")

    sent_count = 0
    skipped_count = 0

    for invoice in overdue_invoices:
        try:
            success, _ = send_invoice_reminder(invoice)
            if success:
                sent_count += 1
            else:
                skipped_count += 1
        except Exception as exc:
            logger.error("Error processing reminder for invoice %s: %s", invoice.id, exc)

    logger.info("Overdue reminders run: %s sent, %s skipped", sent_count, skipped_count)
    return {"sent": sent_count, "skipped": skipped_count}
