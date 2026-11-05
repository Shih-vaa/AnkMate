from django.contrib import admin

from accounts.tenant_admin import TenantAdminMixin

from .models import FeeHead, FeeStructure


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
