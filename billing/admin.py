from django.contrib import admin

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


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "business",
        "customer",
        "status",
        "total",
        "amount_paid",
        "amount_credited",
        "due_date",
    )
    list_filter = ("status", "business")
    inlines = [InvoiceItemInline]


class CreditNoteItemInline(admin.TabularInline):
    model = CreditNoteItem
    extra = 0


@admin.register(CreditNote)
class CreditNoteAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "business",
        "invoice",
        "customer",
        "total",
        "applied_amount",
        "refund_amount",
        "refund_method",
        "issue_date",
    )
    list_filter = ("refund_method", "business")
    inlines = [CreditNoteItemInline]


@admin.register(PaymentReminderLog)
class PaymentReminderLogAdmin(admin.ModelAdmin):
    list_display = ("invoice", "business", "customer", "phone", "status", "sent_at")
    list_filter = ("status", "business")


admin.site.register(Business)
admin.site.register(StaffProfile)
admin.site.register(Customer)
admin.site.register(Product)
admin.site.register(Payment)
