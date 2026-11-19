from django.contrib import admin

from .models import Business, Customer, Invoice, InvoiceItem, Payment, Product, StaffProfile


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "business", "customer", "status", "total", "amount_paid", "due_date")
    list_filter = ("status", "business")
    inlines = [InvoiceItemInline]


admin.site.register(Business)
admin.site.register(StaffProfile)
admin.site.register(Customer)
admin.site.register(Product)
admin.site.register(Payment)
