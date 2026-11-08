from django.contrib import admin

from accounts.tenant_admin import TenantAdminMixin

from .models import Notice


class ReadOnlyMixin:
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notice)
class NoticeAdmin(ReadOnlyMixin, TenantAdminMixin, admin.ModelAdmin):
    """A record of what the school announced. Notices are written, corrected and withdrawn on the app's own screens
    (where the audience and the author are checked), so nobody edits them here."""

    manage_perm = "school.manage"
    view_perm = "notices.post"
    list_display = ("title", "category", "author_name", "posted_at", "expires_on", "withdrawn_at")
    list_filter = ("category",)
    search_fields = ("title", "author_name")
