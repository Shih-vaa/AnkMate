"""Multi-school foundation.

Every school-owned table inherits TenantModel and carries a `school` foreign key.
One deployment can host many schools: adding another is just another row.
"""
from django.core.exceptions import ValidationError
from django.db import models


class School(models.Model):
    class Board(models.TextChoices):
        ICSE = "ICSE", "ICSE / ISC (CISCE)"
        CBSE = "CBSE", "CBSE"
        STATE = "STATE", "State board"
        OTHER = "OTHER", "Other"

    name = models.CharField(max_length=200)
    code = models.SlugField(max_length=50, unique=True, help_text="Short unique id, e.g. maple-grove-demo")
    board = models.CharField(max_length=10, choices=Board.choices, default=Board.ICSE)
    address = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    # Per-school switches and look-and-feel that don't deserve their own column yet.
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TenantQuerySet(models.QuerySet):
    def for_school(self, school):
        return self.filter(school=school)


class TenantModel(models.Model):
    """Abstract base: the row belongs to exactly one school.

    Models list the foreign keys that must point into the *same* school in `tenant_links`.
    The check runs in clean() (forms/admin) and in save() (scripts, imports), so a row can
    never quietly reference another school's data.
    """

    school = models.ForeignKey("core.School", on_delete=models.PROTECT, related_name="+")

    tenant_links = ()
    objects = TenantQuerySet.as_manager()

    class Meta:
        abstract = True

    def check_tenant_links(self):
        if self.school_id is None:
            return
        for name in self.tenant_links:
            if getattr(self, f"{name}_id", None) is None:
                continue
            related = getattr(self, name)
            if related.school_id != self.school_id:
                raise ValidationError({name: "This belongs to a different school."})

    def clean(self):
        super().clean()
        self.check_tenant_links()

    def save(self, *args, **kwargs):
        self.check_tenant_links()
        super().save(*args, **kwargs)
