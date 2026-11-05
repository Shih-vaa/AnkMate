from django.contrib import admin

from accounts.tenant_admin import TenantAdminMixin

from .models import AttendanceRecord, Holiday


@admin.register(Holiday)
class HolidayAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    view_perm = "attendance.view"
    list_display = ("date", "name")
    date_hierarchy = "date"


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(TenantAdminMixin, admin.ModelAdmin):
    """For office corrections only; day-to-day marking is on the Attendance page."""

    manage_perm = "attendance.mark"
    view_perm = "attendance.view"
    list_display = ("date", "student", "section", "status", "marked_by")
    list_filter = ("status", "section__grade", "date")
    search_fields = ("student__first_name", "student__last_name", "student__admission_no")
    date_hierarchy = "date"
