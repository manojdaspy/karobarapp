import logging
from decimal import Decimal

import requests
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

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

    invoice = Invoice.objects.select_related("customer").get(pk=payload["invoice_id"])
    amount = Decimal(str(payload["amount"]))

    record_payment(
        invoice=invoice,
        amount=amount,
        method=Payment.Method.GATEWAY,
        gateway_ref=payload["transaction_id"],
    )
    logger.info("Recorded gateway payment %s for invoice %s", payload["transaction_id"], invoice.pk)

    if invoice.customer.phone:
        send_sms(
            invoice.customer.phone,
            f"Payment of Rs. {amount} received for invoice #{invoice.number}. Thank you!",
        )
