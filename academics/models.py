"""Academic structure. All of it is data: a school's classes, sections and subjects are rows,
so an ICSE school, a CBSE school or a government school only differ in what is loaded here."""
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from core.models import TenantModel


class AcademicYear(TenantModel):
    name = models.CharField(max_length=20, help_text="e.g. 2026-27")
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="uniq_year_name_per_school"),
            models.UniqueConstraint(fields=["school"], condition=Q(is_current=True), name="one_current_year_per_school"),
        ]

    def save(self, *args, **kwargs):
        if self.is_current:
            AcademicYear.objects.filter(school_id=self.school_id, is_current=True).exclude(pk=self.pk).update(is_current=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Grade(TenantModel):
    """A class level, e.g. 'Class V'. (Named Grade to avoid clashing with Python's `class`.)"""

    name = models.CharField(max_length=40)
    order = models.PositiveSmallIntegerField(help_text="Sort order, lowest first")
    stage = models.CharField(max_length=40, blank=True, help_text="e.g. Pre-primary, Primary, Middle, Secondary")

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="uniq_grade_name_per_school"),
            models.UniqueConstraint(fields=["school", "order"], name="uniq_grade_order_per_school"),
        ]

    def __str__(self):
        return self.name


class Subject(TenantModel):
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "code"], name="uniq_subject_code_per_school")]

    def __str__(self):
        return self.name


class GradeSubject(TenantModel):
    """Which subjects are taught in which class."""

    tenant_links = ("grade", "subject")

    grade = models.ForeignKey(Grade, on_delete=models.CASCADE, related_name="grade_subjects")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="grade_subjects")
    is_optional = models.BooleanField(default=False)

    class Meta:
        ordering = ["grade__order", "subject__name"]
        constraints = [models.UniqueConstraint(fields=["grade", "subject"], name="uniq_grade_subject")]

    def __str__(self):
        return f"{self.grade} - {self.subject}"


class Section(TenantModel):
    """A division of a class in a particular academic year, e.g. 'Class V - A' in 2026-27."""

    tenant_links = ("academic_year", "grade", "class_teacher")

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="sections")
    grade = models.ForeignKey(Grade, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=10)
    class_teacher = models.ForeignKey("people.Staff", null=True, blank=True, on_delete=models.SET_NULL, related_name="class_sections")

    class Meta:
        ordering = ["grade__order", "name"]
        constraints = [models.UniqueConstraint(fields=["academic_year", "grade", "name"], name="uniq_section")]

    def __str__(self):
        return f"{self.grade.name}-{self.name}"


class Enrollment(TenantModel):
    """Which section a student is in during a given year. History is kept: one row per year."""

    tenant_links = ("student", "section", "academic_year")

    student = models.ForeignKey("people.Student", on_delete=models.CASCADE, related_name="enrollments")
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="enrollments")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="enrollments", editable=False)
    roll_no = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["section__grade__order", "section__name", "roll_no"]
        constraints = [models.UniqueConstraint(fields=["student", "academic_year"], name="uniq_enrollment_per_year")]

    def clean(self):
        super().clean()
        if self.section_id and self.student_id:
            clash = Enrollment.objects.filter(
                student_id=self.student_id, academic_year_id=self.section.academic_year_id
            ).exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError("This student is already enrolled in a section for that academic year.")

    def save(self, *args, **kwargs):
        self.academic_year_id = self.section.academic_year_id  # always derived from the section
        self.school_id = self.section.school_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student} in {self.section} ({self.academic_year})"


class TeachingAssignment(TenantModel):
    """Teacher X teaches subject Y to section Z. This is what scopes a teacher's access."""

    tenant_links = ("section", "subject", "teacher")

    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="assignments")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="assignments")
    teacher = models.ForeignKey("people.Staff", on_delete=models.CASCADE, related_name="teaching_assignments")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["section", "subject"], name="uniq_teacher_per_section_subject")]

    def __str__(self):
        return f"{self.teacher} teaches {self.subject} to {self.section}"
