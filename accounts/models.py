import re

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models

from core.models import TenantModel

from .permissions import PERMISSIONS, SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_CHILDREN, SCOPE_SELF


def normalize_phone(raw):
    """Keep the last 10 digits so '+91 98765-43210' and '9876543210' are the same login."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if digits else ""


class User(AbstractUser):
    """One account per human. What they can do in a school comes from their Memberships."""

    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    # True while the person is still using a temporary password the school office gave them.
    must_change_password = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone) or None
        super().save(*args, **kwargs)


class Role(TenantModel):
    """A named bundle of permissions plus a scope, defined per school."""

    class Scope(models.TextChoices):
        ALL = SCOPE_ALL, "Whole school"
        ASSIGNED = SCOPE_ASSIGNED, "Assigned classes / sections"
        CHILDREN = SCOPE_CHILDREN, "Own children"
        SELF = SCOPE_SELF, "Self only"

    code = models.SlugField(max_length=40)
    name = models.CharField(max_length=80)
    scope = models.CharField(max_length=10, choices=Scope.choices)
    permissions = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "code"], name="uniq_role_code_per_school")]
        ordering = ["school", "name"]

    def __str__(self):
        return f"{self.name} ({self.school.name})"

    def clean(self):
        super().clean()
        if not isinstance(self.permissions, list):
            raise ValidationError({"permissions": "Must be a list of permission codes."})
        unknown = sorted(set(self.permissions) - set(PERMISSIONS))
        if unknown:
            raise ValidationError({"permissions": f"Unknown permission codes: {', '.join(unknown)}"})


class Membership(TenantModel):
    """Gives a user a role in a school. A person may hold several (e.g. teacher AND parent)."""

    tenant_links = ("role",)

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="memberships")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "role"], name="uniq_membership_user_role")]

    def save(self, *args, **kwargs):
        self.school_id = self.role.school_id  # a membership can never disagree with its role's school
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} - {self.role.name}"
