from django.contrib import admin

from accounts.tenant_admin import TenantAdminMixin

from .models import Exam, ExamPaper, GradeBand, Mark, ReportRemark


class ReadOnlyMixin:
    """Marks are entered and corrected on the app's own screens, where each change is attributed to a person."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Exam)
class ExamAdmin(TenantAdminMixin, admin.ModelAdmin):
    """Define the year's exams and their weightage. Publishing and locking happen on the Marks > Exams screen."""

    manage_perm = "academics.manage"
    list_display = ("name", "academic_year", "order", "weightage", "starts_on", "is_published", "is_locked")
    list_filter = ("academic_year",)
    readonly_fields = ("is_published", "published_at", "is_locked")


@admin.register(ExamPaper)
class ExamPaperAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    list_display = ("exam", "grade", "subject", "max_marks")
    list_filter = ("exam", "grade")


@admin.register(GradeBand)
class GradeBandAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    list_display = ("label", "min_percent")


@admin.register(Mark)
class MarkAdmin(ReadOnlyMixin, TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "marks.enter"
    view_perm = "marks.view"
    list_display = ("student", "paper", "marks", "absent", "entered_by", "entered_at")
    list_filter = ("paper__exam", "absent")
    search_fields = ("student__first_name", "student__last_name", "student__admission_no")


@admin.register(ReportRemark)
class ReportRemarkAdmin(ReadOnlyMixin, TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "marks.enter"
    view_perm = "marks.view"
    list_display = ("student", "academic_year", "text", "written_by", "written_at")
