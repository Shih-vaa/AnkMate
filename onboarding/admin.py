from django.contrib import admin

from accounts.tenant_admin import TenantAdminMixin

from .models import AccountEvent, ImportBatch


class ReadOnlyMixin:
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccountEvent)
class AccountEventAdmin(ReadOnlyMixin, TenantAdminMixin, admin.ModelAdmin):
    """The audit trail of account actions. Read-only for everyone."""

    manage_perm = "school.manage"
    list_display = ("created_at", "action", "target_label", "actor", "detail")
    list_filter = ("action",)
    search_fields = ("target_label", "detail")


@admin.register(ImportBatch)
class ImportBatchAdmin(ReadOnlyMixin, TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "school.manage"
    list_display = ("created_at", "kind", "filename", "status", "created_by")
    list_filter = ("kind", "status")
