from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Business(models.Model):
    name = models.CharField(max_length=200)
    pan_number = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class StaffProfile(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ACCOUNTANT = "accountant", "Accountant"
        CASHIER = "cashier", "Cashier"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="staff")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CASHIER)

    def __str__(self):
        return f"{self.user} @ {self.business}"


class Customer(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="customers")
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Product(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="products")
    sku = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    stock = models.IntegerField(default=0)

    class Meta:
        unique_together = ("business", "sku")

    def __str__(self):
        return f"{self.sku} - {self.name}"


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="invoices")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="invoices")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    amount_credited = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"], name="unique_invoice_number_per_business"
            )
        ]

    def __str__(self):
        return f"INV-{self.number}"

    @property
    def balance(self):
        return self.total - self.amount_paid - self.amount_credited


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"
        GATEWAY = "gateway", "Online gateway"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=20, choices=Method.choices)
    gateway_ref = models.CharField(max_length=100, blank=True, db_index=True)
    received_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["gateway_ref"],
                condition=~Q(gateway_ref=""),
                name="unique_payment_gateway_ref",
            )
        ]

    def __str__(self):
        return f"{self.amount} for {self.invoice}"


class CreditNote(models.Model):
    class RefundMethod(models.TextChoices):
        INVOICE_OFFSET = "invoice_offset", "Invoice balance offset"
        CASH = "cash", "Cash refund"
        BANK = "bank", "Bank refund"
        STORE_CREDIT = "store_credit", "Store credit"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="credit_notes")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="credit_notes")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="credit_notes")
    number = models.PositiveIntegerField()
    issue_date = models.DateField(default=timezone.localdate)
    reason = models.CharField(max_length=255, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    applied_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    refund_method = models.CharField(
        max_length=20, choices=RefundMethod.choices, default=RefundMethod.INVOICE_OFFSET
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"], name="unique_credit_note_number_per_business"
            )
        ]

    def __str__(self):
        return f"CN-{self.number}"


class CreditNoteItem(models.Model):
    credit_note = models.ForeignKey(CreditNote, on_delete=models.CASCADE, related_name="items")
    invoice_item = models.ForeignKey(
        InvoiceItem, on_delete=models.PROTECT, related_name="returned_items"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} x {self.product.sku} for {self.credit_note}"


class PaymentReminderLog(models.Model):
    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED_QUIET_HOURS = "skipped_quiet_hours", "Skipped (Quiet Hours)"
        SKIPPED_COOLDOWN = "skipped_cooldown", "Skipped (Cooldown)"
        SKIPPED_LIMIT = "skipped_limit", "Skipped (Limit Reached)"
        SKIPPED_NOT_ELIGIBLE = "skipped_not_eligible", "Skipped (Not Eligible)"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="reminder_logs")
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="reminder_logs")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    phone = models.CharField(max_length=20)
    message = models.TextField()
    status = models.CharField(max_length=30, choices=Status.choices)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-sent_at"]
        indexes = [
            models.Index(fields=["invoice", "status", "sent_at"]),
        ]

    def __str__(self):
        return f"{self.status} to {self.phone} for {self.invoice}"
