import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch
import zoneinfo

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from .exceptions import InvalidPaymentAmount, ReturnNotAllowed, ReturnQuantityExceeded
from .models import (
    Business,
    CreditNote,
    CreditNoteItem,
    Customer,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentReminderLog,
    Product,
    StaffProfile,
)
from .services import (
    cancel_invoice,
    create_invoice,
    is_quiet_hours,
    process_return,
    record_payment,
    send_invoice_reminder,
)
from .tasks import process_gateway_payment, send_overdue_payment_reminders


class BillingTestCaseBase(TestCase):
    def setUp(self):
        super().setUp()
        # Create Business A
        self.business_a = Business.objects.create(name="Shop Alpha", pan_number="123456789")
        self.user_a = User.objects.create_user(username="cashier_a", password="password123")
        self.profile_a = StaffProfile.objects.create(
            user=self.user_a, business=self.business_a, role=StaffProfile.Role.CASHIER
        )

        # Create Business B (for cross-tenant testing)
        self.business_b = Business.objects.create(name="Shop Beta", pan_number="987654321")
        self.user_b = User.objects.create_user(username="cashier_b", password="password123")
        self.profile_b = StaffProfile.objects.create(
            user=self.user_b, business=self.business_b, role=StaffProfile.Role.CASHIER
        )

        # Customers
        self.customer_a = Customer.objects.create(
            business=self.business_a,
            name="Ram Bahadur",
            phone="9841000000",
            email="ram@example.com",
        )
        self.customer_b = Customer.objects.create(
            business=self.business_b,
            name="Shyam Lal",
            phone="9842000000",
            email="shyam@example.com",
        )

        # Products
        self.product_a1 = Product.objects.create(
            business=self.business_a,
            sku="SKU-CHAIR",
            name="Ergonomic Chair",
            unit_price=Decimal("1000.00"),
            stock=50,
        )
        self.product_a2 = Product.objects.create(
            business=self.business_a,
            sku="SKU-DESK",
            name="Standing Desk",
            unit_price=Decimal("5000.00"),
            stock=20,
        )

        # API Client for User A
        self.client_a = APIClient()
        self.client_a.force_authenticate(user=self.user_a)

        # API Client for User B
        self.client_b = APIClient()
        self.client_b.force_authenticate(user=self.user_b)


class TestReturnsAndCreditNotes(BillingTestCaseBase):
    @patch("billing.tasks.send_invoice_email.delay")
    def test_partial_return_unpaid_invoice(self, mock_email):
        """Customer returns 1 out of 5 chairs on an unpaid invoice."""
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 5}],
            user=self.user_a,
        )
        self.assertEqual(invoice.total, Decimal("5650.00"))  # 5000 + 13% VAT
        self.assertEqual(invoice.balance, Decimal("5650.00"))
        self.product_a1.refresh_from_db()
        self.assertEqual(self.product_a1.stock, 45)

        inv_item = invoice.items.first()
        credit_note = process_return(
            invoice=invoice,
            items=[{"invoice_item_id": inv_item.id, "quantity": 1}],
            reason="Customer changed mind",
            user=self.user_a,
        )

        # Stock should be restocked +1 -> 46
        self.product_a1.refresh_from_db()
        self.assertEqual(self.product_a1.stock, 46)

        # Credit note values
        self.assertEqual(credit_note.number, 1)
        self.assertEqual(credit_note.subtotal, Decimal("1000.00"))
        self.assertEqual(credit_note.tax, Decimal("130.00"))
        self.assertEqual(credit_note.total, Decimal("1130.00"))
        self.assertEqual(credit_note.applied_amount, Decimal("1130.00"))
        self.assertEqual(credit_note.refund_amount, Decimal("0.00"))

        # Invoice balance updated
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_credited, Decimal("1130.00"))
        self.assertEqual(invoice.balance, Decimal("4520.00"))

    @patch("billing.tasks.send_invoice_email.delay")
    def test_full_return_unpaid_invoice(self, mock_email):
        """Customer returns all items on an unpaid invoice; balance becomes 0 and status is PAID."""
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 2}],
            user=self.user_a,
        )
        inv_item = invoice.items.first()
        credit_note = process_return(
            invoice=invoice,
            items=[{"invoice_item_id": inv_item.id, "quantity": 2}],
            reason="All defective",
            user=self.user_a,
        )

        self.assertEqual(credit_note.applied_amount, Decimal("2260.00"))
        self.assertEqual(credit_note.refund_amount, Decimal("0.00"))

        invoice.refresh_from_db()
        self.assertEqual(invoice.balance, Decimal("0.00"))
        self.assertEqual(invoice.status, Invoice.Status.PAID)

        self.product_a1.refresh_from_db()
        self.assertEqual(self.product_a1.stock, 50)  # Fully restocked

    @patch("billing.tasks.send_invoice_email.delay")
    def test_return_on_fully_paid_invoice(self, mock_email):
        """Customer returns goods after already paying in cash; receives cash refund."""
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 2}],
            user=self.user_a,
        )
        record_payment(
            invoice=invoice,
            amount=Decimal("2260.00"),
            method=Payment.Method.CASH,
            user=self.user_a,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(invoice.balance, Decimal("0.00"))

        inv_item = invoice.items.first()
        credit_note = process_return(
            invoice=invoice,
            items=[{"invoice_item_id": inv_item.id, "quantity": 1}],
            reason="Not needed",
            refund_method=CreditNote.RefundMethod.CASH,
            user=self.user_a,
        )

        self.assertEqual(credit_note.applied_amount, Decimal("0.00"))
        self.assertEqual(credit_note.refund_amount, Decimal("1130.00"))
        self.assertEqual(credit_note.refund_method, CreditNote.RefundMethod.CASH)

        invoice.refresh_from_db()
        self.assertEqual(invoice.balance, Decimal("0.00"))
        self.product_a1.refresh_from_db()
        self.assertEqual(self.product_a1.stock, 49)

    @patch("billing.tasks.send_invoice_email.delay")
    def test_return_exceeding_outstanding_balance_edge_case(self, mock_email):
        """
        The critical edge case:
        Invoice = Rs. 11,300 (10 chairs).
        Customer paid Rs. 8,000. Balance remaining = Rs. 3,300.
        Customer returns 4 chairs (value = 4000 + 13% = Rs. 4,520).
        Applied = Rs. 3,300 (absorbs balance to 0).
        Refund = Rs. 1,220 (owed back to customer).
        """
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 10}],
            user=self.user_a,
        )
        self.assertEqual(invoice.total, Decimal("11300.00"))

        record_payment(
            invoice=invoice,
            amount=Decimal("8000.00"),
            method=Payment.Method.BANK,
            user=self.user_a,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.balance, Decimal("3300.00"))

        inv_item = invoice.items.first()
        credit_note = process_return(
            invoice=invoice,
            items=[{"invoice_item_id": inv_item.id, "quantity": 4}],
            reason="Partial return with excess refund",
            refund_method=CreditNote.RefundMethod.CASH,
            user=self.user_a,
        )

        self.assertEqual(credit_note.total, Decimal("4520.00"))
        self.assertEqual(credit_note.applied_amount, Decimal("3300.00"))
        self.assertEqual(credit_note.refund_amount, Decimal("1220.00"))

        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_credited, Decimal("3300.00"))
        self.assertEqual(invoice.balance, Decimal("0.00"))
        self.assertEqual(invoice.status, Invoice.Status.PAID)

    @patch("billing.tasks.send_invoice_email.delay")
    def test_return_quantity_exceeds_purchased_rejected(self, mock_email):
        """Cannot return more than invoiced quantity."""
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 3}],
            user=self.user_a,
        )
        inv_item = invoice.items.first()

        with self.assertRaises(ReturnQuantityExceeded):
            process_return(
                invoice=invoice,
                items=[{"invoice_item_id": inv_item.id, "quantity": 4}],
                user=self.user_a,
            )

    @patch("billing.tasks.send_invoice_email.delay")
    def test_multiple_partial_returns_cumulative_check(self, mock_email):
        """Multiple returns cannot cumulatively exceed original quantity."""
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 5}],
            user=self.user_a,
        )
        inv_item = invoice.items.first()

        # First return: 3 units -> OK
        process_return(
            invoice=invoice,
            items=[{"invoice_item_id": inv_item.id, "quantity": 3}],
            user=self.user_a,
        )

        # Second return: 3 units -> Rejected (only 2 left)
        with self.assertRaises(ReturnQuantityExceeded):
            process_return(
                invoice=invoice,
                items=[{"invoice_item_id": inv_item.id, "quantity": 3}],
                user=self.user_a,
            )

        # Second return: 2 units -> OK
        cn2 = process_return(
            invoice=invoice,
            items=[{"invoice_item_id": inv_item.id, "quantity": 2}],
            user=self.user_a,
        )
        self.assertEqual(cn2.number, 2)
        invoice.refresh_from_db()
        self.assertEqual(invoice.balance, Decimal("0.00"))

    @patch("billing.tasks.send_invoice_email.delay")
    def test_cannot_return_cancelled_invoice(self, mock_email):
        """Cannot process return for an already cancelled invoice."""
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 1}],
            user=self.user_a,
        )
        cancel_invoice(invoice)

        inv_item = invoice.items.first()
        with self.assertRaises(ReturnNotAllowed):
            process_return(
                invoice=invoice,
                items=[{"invoice_item_id": inv_item.id, "quantity": 1}],
                user=self.user_a,
            )

    @patch("billing.tasks.send_invoice_email.delay")
    def test_cross_tenant_return_rejected_via_api(self, mock_email):
        """Business B staff cannot return goods against Business A's invoice."""
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 2}],
            user=self.user_a,
        )
        inv_item = invoice.items.first()

        url = f"/api/invoices/{invoice.id}/returns/"
        payload = {
            "reason": "Hacking return",
            "items": [{"invoice_item_id": inv_item.id, "quantity": 1}],
        }
        response = self.client_b.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TestPaymentReminders(BillingTestCaseBase):
    def setUp(self):
        super().setUp()
        self.overdue_invoice = Invoice.objects.create(
            business=self.business_a,
            customer=self.customer_a,
            number=101,
            status=Invoice.Status.ISSUED,
            issue_date=timezone.localdate() - datetime.timedelta(days=10),
            due_date=timezone.localdate() - datetime.timedelta(days=3),  # Overdue
            subtotal=Decimal("2000.00"),
            tax=Decimal("260.00"),
            total=Decimal("2260.00"),
            amount_paid=Decimal("0.00"),
            created_by=self.user_a,
        )

    @patch("billing.services.is_quiet_hours", return_value=False)
    @patch("billing.services.send_sms")
    def test_daytime_eligible_dispatch(self, mock_sms, mock_quiet):
        """Eligible overdue invoice during daytime dispatches SMS and logs SENT."""
        success, msg = send_invoice_reminder(self.overdue_invoice)
        self.assertTrue(success)
        mock_sms.assert_called_once()

        log = PaymentReminderLog.objects.filter(invoice=self.overdue_invoice).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, PaymentReminderLog.Status.SENT)
        self.assertEqual(log.phone, self.customer_a.phone)

    @patch("billing.services.is_quiet_hours", return_value=True)
    @patch("billing.services.send_sms")
    def test_quiet_hours_suppression(self, mock_sms, mock_quiet):
        """During quiet hours, SMS is suppressed and logged as SKIPPED_QUIET_HOURS."""
        success, msg = send_invoice_reminder(self.overdue_invoice)
        self.assertFalse(success)
        mock_sms.assert_not_called()

        log = PaymentReminderLog.objects.filter(invoice=self.overdue_invoice).first()
        self.assertEqual(log.status, PaymentReminderLog.Status.SKIPPED_QUIET_HOURS)

    @patch("billing.services.is_quiet_hours", return_value=False)
    @patch("billing.services.send_sms")
    def test_anti_spam_24h_cooldown(self, mock_sms, mock_quiet):
        """Second reminder within 24 hours is blocked and logged as SKIPPED_COOLDOWN."""
        # First send succeeds
        success1, _ = send_invoice_reminder(self.overdue_invoice)
        self.assertTrue(success1)
        self.assertEqual(mock_sms.call_count, 1)

        # Immediate second attempt
        success2, msg = send_invoice_reminder(self.overdue_invoice)
        self.assertFalse(success2)
        self.assertIn("cooldown active", msg.lower())
        self.assertEqual(mock_sms.call_count, 1)  # Not called again!

        logs = PaymentReminderLog.objects.filter(invoice=self.overdue_invoice).order_by("id")
        self.assertEqual(logs[0].status, PaymentReminderLog.Status.SENT)
        self.assertEqual(logs[1].status, PaymentReminderLog.Status.SKIPPED_COOLDOWN)

    @patch("billing.services.is_quiet_hours", return_value=False)
    @patch("billing.services.send_sms")
    def test_max_3_reminders_limit(self, mock_sms, mock_quiet):
        """Cannot send more than 3 successful reminders for one invoice."""
        # Create 3 past sent logs, each 25 hours apart
        now = timezone.now()
        for i in range(3, 0, -1):
            PaymentReminderLog.objects.create(
                business=self.business_a,
                invoice=self.overdue_invoice,
                customer=self.customer_a,
                phone=self.customer_a.phone,
                message="Test",
                status=PaymentReminderLog.Status.SENT,
                sent_at=now - datetime.timedelta(hours=i * 25),
            )

        # 4th attempt
        success, msg = send_invoice_reminder(self.overdue_invoice)
        self.assertFalse(success)
        self.assertIn("maximum of 3 reminders", msg.lower())

        latest_log = PaymentReminderLog.objects.filter(invoice=self.overdue_invoice).latest("sent_at")
        self.assertEqual(latest_log.status, PaymentReminderLog.Status.SKIPPED_LIMIT)

    @patch("billing.services.is_quiet_hours", return_value=False)
    @patch("billing.services.send_sms", side_effect=Exception("SMS Provider 503"))
    def test_failed_sms_does_not_consume_quota(self, mock_sms, mock_quiet):
        """If SMS gateway fails, it logs FAILED and does not consume the SENT quota."""
        success, msg = send_invoice_reminder(self.overdue_invoice)
        self.assertFalse(success)

        log = PaymentReminderLog.objects.filter(invoice=self.overdue_invoice).first()
        self.assertEqual(log.status, PaymentReminderLog.Status.FAILED)

        # Confirm 0 SENT logs
        sent_count = PaymentReminderLog.objects.filter(
            invoice=self.overdue_invoice, status=PaymentReminderLog.Status.SENT
        ).count()
        self.assertEqual(sent_count, 0)

    @patch("billing.services.is_quiet_hours", return_value=False)
    @patch("billing.services.send_sms")
    def test_scheduled_task_processes_overdue_invoices(self, mock_sms, mock_quiet):
        """Celery periodic task processes all overdue invoices."""
        result = send_overdue_payment_reminders()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 0)


class TestSecurityAndHardening(BillingTestCaseBase):
    @patch("billing.tasks.send_invoice_email.delay")
    def test_sql_injection_defense_in_outstanding_report(self, mock_email):
        """Testing SQL injection search strings against the outstanding report endpoint."""
        create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 1}],
            user=self.user_a,
        )

        malicious_input = "' OR '1'='1"
        url = f"/api/reports/outstanding/?q={malicious_input}"
        response = self.client_a.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should safely find 0 customers with that literal substring
        self.assertEqual(len(response.data), 0)

        # Normal search finds customer
        url_normal = "/api/reports/outstanding/?q=Ram"
        response_normal = self.client_a.get(url_normal)
        self.assertEqual(response_normal.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response_normal.data), 1)
        self.assertEqual(response_normal.data[0]["name"], "Ram Bahadur")

    @patch("billing.tasks.send_invoice_email.delay")
    def test_payment_cross_tenant_idor_rejected(self, mock_email):
        """Shop B cannot post payments to Shop A's invoice."""
        invoice_a = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 1}],
            user=self.user_a,
        )

        url = f"/api/invoices/{invoice_a.id}/payments/"
        payload = {"amount": "500.00", "method": "cash"}
        response = self.client_b.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("billing.tasks.send_invoice_email.delay")
    def test_payment_cannot_exceed_balance_or_be_negative(self, mock_email):
        """Payment validation prevents overpayment and negative amounts."""
        invoice = create_invoice(
            business=self.business_a,
            customer=self.customer_a,
            due_date=timezone.localdate() + datetime.timedelta(days=7),
            items=[{"product_id": self.product_a1.id, "quantity": 1}],
            user=self.user_a,
        )
        self.assertEqual(invoice.total, Decimal("1130.00"))

        # Negative amount rejected by serializer
        url = f"/api/invoices/{invoice.id}/payments/"
        res_neg = self.client_a.post(url, {"amount": "-50.00", "method": "cash"}, format="json")
        self.assertEqual(res_neg.status_code, status.HTTP_400_BAD_REQUEST)

        # Overpayment rejected by service
        res_over = self.client_a.post(url, {"amount": "2000.00", "method": "cash"}, format="json")
        self.assertEqual(res_over.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exceeds", res_over.data["detail"].lower())

    @patch("billing.tasks.send_sms")
    def test_gateway_webhook_idempotency(self, mock_sms):
        """Webhook task ignores duplicate transaction IDs cleanly."""
        invoice = Invoice.objects.create(
            business=self.business_a,
            customer=self.customer_a,
            number=202,
            status=Invoice.Status.ISSUED,
            issue_date=timezone.localdate(),
            due_date=timezone.localdate() + datetime.timedelta(days=5),
            total=Decimal("5000.00"),
            amount_paid=Decimal("0.00"),
        )

        payload = {
            "invoice_id": invoice.id,
            "amount": "2000.00",
            "transaction_id": "TXN-UNIQUE-999",
        }

        # First webhook execution
        process_gateway_payment(payload)
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, Decimal("2000.00"))
        self.assertEqual(Payment.objects.filter(gateway_ref="TXN-UNIQUE-999").count(), 1)

        # Second replay of the exact same webhook
        process_gateway_payment(payload)
        invoice.refresh_from_db()
        # Amount paid should still be 2000, not 4000!
        self.assertEqual(invoice.amount_paid, Decimal("2000.00"))
        self.assertEqual(Payment.objects.filter(gateway_ref="TXN-UNIQUE-999").count(), 1)
