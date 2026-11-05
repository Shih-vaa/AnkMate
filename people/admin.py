from django.contrib import admin

from accounts.tenant_admin import TenantAdminMixin

from .models import Guardian, Staff, Student, StudentGuardian


@admin.register(Student)
class StudentAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "students.manage"
    view_perm = "students.view"
    list_display = ("admission_no", "first_name", "last_name", "gender", "date_of_birth", "status")
    list_filter = ("status", "gender")
    search_fields = ("admission_no", "first_name", "last_name")


@admin.register(Guardian)
class GuardianAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "students.manage"
    view_perm = "students.view"
    list_display = ("first_name", "last_name", "phone", "email")
    search_fields = ("first_name", "last_name", "phone", "email")


@admin.register(StudentGuardian)
class StudentGuardianAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "students.manage"
    view_perm = "students.view"
    list_display = ("student", "guardian", "relation", "is_primary")
    list_filter = ("relation", "is_primary")


@admin.register(Staff)
class StaffAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "staff.manage"
    view_perm = "staff.view"
    list_display = ("employee_code", "first_name", "last_name", "designation", "phone", "is_active")
    list_filter = ("is_active", "designation")
    search_fields = ("employee_code", "first_name", "last_name", "phone")
