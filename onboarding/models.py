"""Getting a school's people into the system: an audit log of account actions, and staged CSV imports."""
from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TenantModel


class AccountEvent(TenantModel):
    """Who created, reset, deactivated or reactivated whose login, and when. Never edited or deleted by the app."""

    class Action(models.TextChoices):
        CREATED = "created", "Login created"
        LINKED = "linked", "Linked to an existing login"
        RESET = "reset", "Password reset"
        DEACTIVATED = "deactivated", "Login deactivated"
        ACTIVATED = "activated", "Login reactivated"

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    target = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="account_events")
    target_label = models.CharField(max_length=200, help_text="The person's name when it happened")
    action = models.CharField(max_length=12, choices=Action.choices)
    detail = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["school", "-created_at"], name="acct_event_school_time_idx")]

    def __str__(self):
        return f"{self.get_action_display()}: {self.target_label}"


class ImportBatch(TenantModel):
    """An uploaded spreadsheet that has been read and checked but not yet applied, or has been applied.

    Rows are validated once and stored in cleaned form, so what the office previewed is what gets imported."""

    class Kind(models.TextChoices):
        STUDENTS = "students", "Students and their parents"
        STAFF = "staff", "Staff"

    class Status(models.TextChoices):
        PREVIEW = "preview", "Waiting for confirmation"
        APPLIED = "applied", "Imported"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    filename = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PREVIEW)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    rows = models.JSONField(default=list, help_text="Cleaned rows ready to import")
    problems = models.JSONField(default=list, help_text="Rows that failed checks, with the reasons")
    summary = models.JSONField(default=dict)
    applied_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.get_kind_display()} import: {self.filename}"
