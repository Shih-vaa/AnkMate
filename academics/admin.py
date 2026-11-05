from django.contrib import admin

from accounts.tenant_admin import TenantAdminMixin

from .models import AcademicYear, Enrollment, Grade, GradeSubject, Section, Subject, TeachingAssignment


@admin.register(AcademicYear)
class AcademicYearAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    list_display = ("name", "start_date", "end_date", "is_current")


@admin.register(Grade)
class GradeAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    list_display = ("name", "order", "stage")


@admin.register(Subject)
class SubjectAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    list_display = ("name", "code")
    search_fields = ("name", "code")


@admin.register(GradeSubject)
class GradeSubjectAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    list_display = ("grade", "subject", "is_optional")
    list_filter = ("grade",)


@admin.register(Section)
class SectionAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    list_display = ("__str__", "academic_year", "class_teacher")
    list_filter = ("academic_year", "grade")


@admin.register(Enrollment)
class EnrollmentAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "students.manage"
    view_perm = "students.view"
    list_display = ("student", "section", "academic_year", "roll_no")
    list_filter = ("academic_year", "section__grade")
    search_fields = ("student__first_name", "student__last_name", "student__admission_no")


@admin.register(TeachingAssignment)
class TeachingAssignmentAdmin(TenantAdminMixin, admin.ModelAdmin):
    manage_perm = "academics.manage"
    list_display = ("teacher", "subject", "section")
    list_filter = ("section__grade", "subject")
