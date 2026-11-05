from django.contrib import admin

from accounts.tenant_admin import TenantAdminMixin

from .models import Concession, FeeHead, FeeInvoice, FeeStructure, Payment


class ReadOnlyLedgerMixin:
    """Invoices and payments are a ledger: nobody edits or deletes them here, not even the principal.
    Corrections are made on the app's own screens (cancel an invoice, reverse a payment) so each one is recorded."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FeeHead)
class FeeHeadAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "fees.manage"
    view_perm = "fees.view"
    list_display = ("name", "code")


@admin.register(FeeStructure)
class FeeStructureAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "fees.manage"
    view_perm = "fees.view"
    list_display = ("grade", "head", "amount", "frequency", "academic_year")
    list_filter = ("academic_year", "grade", "frequency")


@admin.register(Concession)
class ConcessionAdmin(TenantAdminMixin, admin.ModelAdmin):
    """Scholarships and sibling discounts. They apply to invoices generated *after* they are added."""

    manage_perm = "fees.manage"
    view_perm = "fees.view"
    list_display = ("student", "kind", "value", "head", "reason", "academic_year", "is_active")
    list_filter = ("kind", "is_active", "academic_year")
    search_fields = ("student__first_name", "student__last_name", "student__admission_no", "reason")


@admin.register(FeeInvoice)
class FeeInvoiceAdmin(ReadOnlyLedgerMixin, TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "fees.manage"
    view_perm = "fees.view"
    list_display = ("invoice_no", "student", "month", "due_date", "total", "status")
    list_filter = ("status", "academic_year", "month")
    search_fields = ("invoice_no", "student__first_name", "student__last_name", "student__admission_no")


@admin.register(Payment)
class PaymentAdmin(ReadOnlyLedgerMixin, TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "fees.manage"
    view_perm = "fees.view"
    list_display = ("receipt_no", "student", "amount", "method", "received_on", "status")
    list_filter = ("status", "method", "received_on")
    search_fields = ("receipt_no", "reference", "student__first_name", "student__last_name", "student__admission_no")
