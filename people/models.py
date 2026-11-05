from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantModel


class Gender(models.TextChoices):
    MALE = "M", "Male"
    FEMALE = "F", "Female"
    OTHER = "O", "Other"


class PersonMixin(models.Model):
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True)

    class Meta:
        abstract = True

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name


class Staff(PersonMixin, TenantModel):
    """Teachers and non-teaching staff. `user` links the login account (if they have one)."""

    tenant_links = ()

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="staff_profiles")
    employee_code = models.CharField(max_length=30)
    designation = models.CharField(max_length=80, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    date_joined = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "staff"
        ordering = ["first_name", "last_name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "employee_code"], name="uniq_staff_code_per_school"),
            models.UniqueConstraint(fields=["school", "user"], condition=Q(user__isnull=False), name="uniq_staff_user_per_school"),
        ]


class Student(PersonMixin, TenantModel):
    """A child at the school. Which class they are in lives in academics.Enrollment (one row per year)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        TRANSFERRED = "transferred", "Transferred out"
        ALUMNI = "alumni", "Alumni"
        DROPPED = "dropped", "Left / dropped"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="student_profiles")
    admission_no = models.CharField(max_length=30)
    date_of_birth = models.DateField(null=True, blank=True)
    admission_date = models.DateField(null=True, blank=True)
    address = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ["first_name", "last_name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "admission_no"], name="uniq_admission_no_per_school"),
            models.UniqueConstraint(fields=["school", "user"], condition=Q(user__isnull=False), name="uniq_student_user_per_school"),
        ]


class Guardian(PersonMixin, TenantModel):
    """A parent / guardian. One guardian can be linked to several children (siblings)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="guardian_profiles")
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    occupation = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["first_name", "last_name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "user"], condition=Q(user__isnull=False), name="uniq_guardian_user_per_school"),
        ]


class StudentGuardian(TenantModel):
    tenant_links = ("student", "guardian")

    class Relation(models.TextChoices):
        FATHER = "father", "Father"
        MOTHER = "mother", "Mother"
        GUARDIAN = "guardian", "Guardian"
        OTHER = "other", "Other"

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="guardian_links")
    guardian = models.ForeignKey(Guardian, on_delete=models.CASCADE, related_name="student_links")
    relation = models.CharField(max_length=10, choices=Relation.choices, default=Relation.GUARDIAN)
    is_primary = models.BooleanField(default=False, help_text="Primary contact for school communication")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "guardian"], name="uniq_student_guardian")]

    def __str__(self):
        return f"{self.guardian} -> {self.student} ({self.get_relation_display()})"
