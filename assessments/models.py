"""Exams, marks, grade scale and report-card remarks.

Everything school-specific is data: which exams exist, how much each counts towards the final result, the maximum
marks of each paper, and the grade scale. Marks stay draft (invisible to parents and students) until the exam is
published; a locked exam can no longer be changed by anyone.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TenantModel


class Exam(TenantModel):
    """An assessment in one academic year, e.g. 'Unit Test 1' or 'Half-Yearly'."""

    tenant_links = ("academic_year",)

    academic_year = models.ForeignKey("academics.AcademicYear", on_delete=models.CASCADE, related_name="exams")
    name = models.CharField(max_length=60)
    order = models.PositiveSmallIntegerField(help_text="Position in the year, lowest first")
    weightage = models.DecimalField(
        max_digits=5, decimal_places=2, help_text="How much this exam counts towards the final result, in percent"
    )
    starts_on = models.DateField(null=True, blank=True)
    is_published = models.BooleanField(default=False, help_text="Visible to parents and students")
    published_at = models.DateTimeField(null=True, blank=True)
    is_locked = models.BooleanField(default=False, help_text="Final: nobody can change the marks any more")

    class Meta:
        ordering = ["academic_year", "order"]
        constraints = [
            models.UniqueConstraint(fields=["academic_year", "name"], name="uniq_exam_name_per_year"),
            models.UniqueConstraint(fields=["academic_year", "order"], name="uniq_exam_order_per_year"),
        ]

    def clean(self):
        super().clean()
        if self.weightage is not None and not 0 <= self.weightage <= 100:
            raise ValidationError({"weightage": "Must be between 0 and 100."})

    def __str__(self):
        return f"{self.name} ({self.academic_year})"


class ExamPaper(TenantModel):
    """One subject's paper for one class in an exam, with its maximum marks."""

    tenant_links = ("exam", "grade", "subject")

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="papers")
    grade = models.ForeignKey("academics.Grade", on_delete=models.CASCADE, related_name="exam_papers")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="exam_papers")
    max_marks = models.DecimalField(max_digits=6, decimal_places=2)

    class Meta:
        ordering = ["exam__order", "grade__order", "subject__name"]
        constraints = [
            models.UniqueConstraint(fields=["exam", "grade", "subject"], name="uniq_paper"),
            models.CheckConstraint(condition=Q(max_marks__gt=0), name="paper_max_marks_positive"),
        ]

    def __str__(self):
        return f"{self.exam.name}: {self.grade} {self.subject} (/{self.max_marks})"


class Mark(TenantModel):
    """A student's result in one paper. `absent` is deliberately different from 0 marks; no row means not entered yet."""

    tenant_links = ("paper", "student")

    paper = models.ForeignKey(ExamPaper, on_delete=models.CASCADE, related_name="marks")
    student = models.ForeignKey("people.Student", on_delete=models.CASCADE, related_name="marks")
    marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    absent = models.BooleanField(default=False)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    entered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["paper", "student"], name="uniq_mark_per_paper_student"),
            models.CheckConstraint(condition=Q(absent=False) | Q(marks__isnull=True), name="absent_has_no_marks"),
            models.CheckConstraint(condition=Q(marks__isnull=True) | Q(marks__gte=0), name="marks_not_negative"),
        ]
        indexes = [models.Index(fields=["student", "paper"], name="mark_student_paper_idx")]

    def clean(self):
        super().clean()
        if self.marks is not None and self.paper_id and self.marks > self.paper.max_marks:
            raise ValidationError({"marks": f"Cannot be more than the maximum of {self.paper.max_marks}."})
        if self.absent and self.marks is not None:
            raise ValidationError("A student marked absent cannot also have marks.")

    def __str__(self):
        return f"{self.student}: {'AB' if self.absent else self.marks} in {self.paper}"


class GradeBand(TenantModel):
    """One step of the school's grade scale: a percentage at or above `min_percent` earns `label`."""

    label = models.CharField(max_length=10)
    min_percent = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        ordering = ["-min_percent"]
        constraints = [
            models.UniqueConstraint(fields=["school", "label"], name="uniq_gradeband_label"),
            models.UniqueConstraint(fields=["school", "min_percent"], name="uniq_gradeband_min"),
        ]

    def clean(self):
        super().clean()
        if self.min_percent is not None and not 0 <= self.min_percent <= 100:
            raise ValidationError({"min_percent": "Must be between 0 and 100."})

    def __str__(self):
        return f"{self.label} (from {self.min_percent}%)"


class ReportRemark(TenantModel):
    """The class teacher's overall comment on a student's report card for the year."""

    tenant_links = ("student", "academic_year")

    student = models.ForeignKey("people.Student", on_delete=models.CASCADE, related_name="report_remarks")
    academic_year = models.ForeignKey("academics.AcademicYear", on_delete=models.CASCADE, related_name="report_remarks")
    text = models.CharField(max_length=300)
    written_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    written_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "academic_year"], name="uniq_remark_per_year")]

    def __str__(self):
        return f"{self.student}: {self.text[:40]}"
