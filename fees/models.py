"""Fee structure (what each class is charged). Invoices, payments and receipts come next phase."""
from decimal import Decimal

from django.db import models

from core.models import TenantModel


class FeeHead(TenantModel):
    """A kind of charge: Tuition, Annual Charges, Computer fee, ..."""

    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "code"], name="uniq_feehead_code_per_school")]

    def __str__(self):
        return self.name


class FeeStructure(TenantModel):
    """How much a class pays for one fee head, and how often, in an academic year."""

    tenant_links = ("academic_year", "grade", "head")

    class Frequency(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        HALF_YEARLY = "half_yearly", "Half-yearly"
        ANNUAL = "annual", "Annual"
        ONE_TIME = "one_time", "One-time (at admission)"

    PER_YEAR = {"monthly": 12, "quarterly": 4, "half_yearly": 2, "annual": 1}

    academic_year = models.ForeignKey("academics.AcademicYear", on_delete=models.CASCADE, related_name="fee_structures")
    grade = models.ForeignKey("academics.Grade", on_delete=models.CASCADE, related_name="fee_structures")
    head = models.ForeignKey(FeeHead, on_delete=models.PROTECT, related_name="structures")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    frequency = models.CharField(max_length=12, choices=Frequency.choices, default=Frequency.MONTHLY)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["grade__order", "head__name"]
        constraints = [models.UniqueConstraint(fields=["academic_year", "grade", "head"], name="uniq_fee_structure")]

    @property
    def is_recurring(self):
        return self.frequency != self.Frequency.ONE_TIME

    @property
    def yearly_amount(self):
        """Amount payable across one academic year (one-time fees are excluded)."""
        return self.amount * Decimal(self.PER_YEAR.get(self.frequency, 0))

    def __str__(self):
        return f"{self.grade} {self.head}: {self.amount}/{self.get_frequency_display()}"
