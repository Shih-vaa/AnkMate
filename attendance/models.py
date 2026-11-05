from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TenantModel


class Status(models.TextChoices):
    PRESENT = "P", "Present"
    ABSENT = "A", "Absent"
    LATE = "L", "Late"
    HALF_DAY = "H", "Half day"
    LEAVE = "E", "On leave"


class Holiday(TenantModel):
    """A day the school is closed (besides the weekly off days, which are a school setting)."""

    date = models.DateField()
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["school", "date"], name="uniq_holiday_per_school_date")]

    def __str__(self):
        return f"{self.date:%d %b %Y} - {self.name}"


class AttendanceRecord(TenantModel):
    """One student, one day. `section` is where the student was *that day*, so history survives a class change."""

    tenant_links = ("student", "section")

    student = models.ForeignKey("people.Student", on_delete=models.CASCADE, related_name="attendance_records")
    section = models.ForeignKey("academics.Section", on_delete=models.PROTECT, related_name="attendance_records")
    date = models.DateField()
    status = models.CharField(max_length=1, choices=Status.choices)
    remarks = models.CharField(max_length=200, blank=True)
    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    marked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "date"], name="uniq_attendance_student_date")]
        indexes = [
            models.Index(fields=["section", "date"], name="att_section_date_idx"),
            models.Index(fields=["school", "date"], name="att_school_date_idx"),
        ]

    def __str__(self):
        return f"{self.student} {self.date:%d %b %Y}: {self.get_status_display()}"
