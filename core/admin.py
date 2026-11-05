from django.contrib import admin

from .models import School


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    """Schools are platform-level objects: only the platform owner (superuser) manages them."""

    list_display = ("name", "code", "board", "city", "is_active")
    prepopulated_fields = {"code": ("name",)}

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
