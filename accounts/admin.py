from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Membership, Role, User
from .tenant_admin import TenantAdminMixin


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Accounts are platform-level, so only the platform owner manages them for now.
    (A guardian/teacher 'create login' flow for school staff is a planned feature.)"""

    list_display = ("username", "phone", "email", "first_name", "last_name", "is_staff", "is_active")
    search_fields = ("username", "phone", "email", "first_name", "last_name")
    fieldsets = DjangoUserAdmin.fieldsets + (("Contact", {"fields": ("phone",)}),)
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (("Contact", {"fields": ("phone",)}),)

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


@admin.register(Role)
class RoleAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "school.manage"
    list_display = ("name", "code", "scope", "school")
    list_filter = ("scope",)


@admin.register(Membership)
class MembershipAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "school.manage"
    list_display = ("user", "role", "is_active", "school")
    list_filter = ("role", "is_active")
    search_fields = ("user__username", "user__phone", "user__first_name", "user__last_name")
