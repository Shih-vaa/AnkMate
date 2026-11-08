"""Notices: announcements from the school to an audience (everyone, a role, a class or a section).

Who may read a notice is worked out from its audience rows when it is asked for (see notices.services),
never copied onto each person, so a student who changes class or a new parent is covered without any clean-up.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TenantModel


class Notice(TenantModel):
    class Category(models.TextChoices):
        GENERAL = "general", "General"
        EVENT = "event", "Event"
        HOLIDAY = "holiday", "Holiday"
        EXAM = "exam", "Exams"
        FEES = "fees", "Fees"
        URGENT = "urgent", "Urgent"

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=4000)
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.GENERAL)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="notices_posted")
    author_name = models.CharField(max_length=150, help_text="The author's name when it was posted, kept if the account goes")
    posted_at = models.DateTimeField(default=timezone.now)
    edited_at = models.DateTimeField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True, help_text="Hidden from readers after this day")
    withdrawn_at = models.DateTimeField(null=True, blank=True, help_text="Taken down by its author or the school office")

    class Meta:
        ordering = ["-posted_at", "-pk"]
        indexes = [models.Index(fields=["school", "-posted_at"], name="notice_school_time_idx")]

    def __str__(self):
        return self.title

    @property
    def is_withdrawn(self):
        return self.withdrawn_at is not None

    def is_live(self, today):
        """Readable by its audience: not withdrawn and not past its last day."""
        return self.withdrawn_at is None and (self.expires_on is None or self.expires_on >= today)

    def audience_labels(self):
        """Who it was addressed to, in words: ['Everyone'] or ['Parent / Guardian', 'Class 5', 'Class 6-A']."""
        order = {"school": 0, "role": 1, "grade": 2, "section": 3}
        targets = sorted(self.targets.all(), key=lambda t: (order[t.kind], t.label))
        return [t.label for t in targets] or ["No one (the classes it was sent to were removed)"]


class NoticeTarget(TenantModel):
    """One part of a notice's audience. A notice may have several (for example Class 5 and the teachers)."""

    class Kind(models.TextChoices):
        SCHOOL = "school", "Everyone in the school"
        ROLE = "role", "Everyone with a role"
        GRADE = "grade", "A whole class"
        SECTION = "section", "One section"

    tenant_links = ("notice", "role", "grade", "section")

    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="targets")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    role = models.ForeignKey("accounts.Role", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    grade = models.ForeignKey("academics.Grade", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    section = models.ForeignKey("academics.Section", null=True, blank=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            # Exactly the one column that goes with the kind is filled in.
            models.CheckConstraint(
                name="notice_target_matches_kind",
                condition=(
                    Q(kind="school", role__isnull=True, grade__isnull=True, section__isnull=True)
                    | Q(kind="role", role__isnull=False, grade__isnull=True, section__isnull=True)
                    | Q(kind="grade", role__isnull=True, grade__isnull=False, section__isnull=True)
                    | Q(kind="section", role__isnull=True, grade__isnull=True, section__isnull=False)
                ),
            )
        ]

    def __str__(self):
        return f"{self.notice}: {self.label}"

    @property
    def label(self):
        if self.kind == self.Kind.ROLE:
            return self.role.name
        if self.kind == self.Kind.GRADE:
            return self.grade.name
        if self.kind == self.Kind.SECTION:
            return f"{self.section.grade.name}-{self.section.name}"
        return "Everyone"


class NoticeRead(TenantModel):
    """Who has opened which notice (so the author can see how far it reached, and readers see what is new)."""

    tenant_links = ("notice",)

    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["notice", "user"], name="uniq_notice_read_per_user")]


class Reminder(TenantModel):
    """A message for one person: a fee that is overdue, a child marked absent. Notices go to an audience;
    reminders go to a single login, and each can be opened (which marks it read) or left."""

    class Kind(models.TextChoices):
        FEE = "fee", "Fee reminder"
        ABSENCE = "absence", "Absence"
        ABSENCE_FIX = "absence_fix", "Attendance corrected"

    tenant_links = ("student",)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reminders")
    student = models.ForeignKey("people.Student", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    title = models.CharField(max_length=120)
    body = models.CharField(max_length=400)
    link = models.CharField(max_length=200, blank=True, help_text="A page of this app to open, such as /fees/student/5/")
    key = models.CharField(max_length=100, blank=True, help_text="Stops the same reminder being sent twice")
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["user", "school", "read_at"], name="reminder_user_unread_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "user", "key"], condition=~Q(key=""), name="uniq_reminder_key_per_user"
            )
        ]

    def __str__(self):
        return f"{self.title} (to {self.user})"

    @property
    def is_read(self):
        return self.read_at is not None
