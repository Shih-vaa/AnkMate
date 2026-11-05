"""Fees: the structure (what each class is charged) and the ledger (invoices, payments, receipts).

MONEY RULES
  * Amounts are Decimal with 2 places, never floats.
  * The ledger is append-only. An issued invoice's amounts and a payment's amount can never be edited;
    mistakes are fixed by CANCELLING an invoice or REVERSING a payment, each with a recorded reason.
  * Balances are never stored: they are always computed from invoices and completed payment allocations.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TenantModel


class FeeError(ValueError):
    """A fee rule was broken. The message is written to be shown to the person who triggered it."""


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


# ======================================================================================
# Ledger
# ======================================================================================

class NumberSequence(TenantModel):
    """Gap-free counters for invoice and receipt numbers (one row per school per series)."""

    key = models.CharField(max_length=60)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "key"], name="uniq_sequence_per_school")]

    def __str__(self):
        return f"{self.key}: {self.last_value}"


class Concession(TenantModel):
    """A standing discount for one student: scholarship, sibling discount, staff child, ...

    Applied automatically when invoices are generated. PERCENT is a share of the matching charges on each
    invoice; FIXED is a rupee amount taken off each invoice. `head` empty means "all fee heads"."""

    tenant_links = ("student", "academic_year", "head")

    class Kind(models.TextChoices):
        PERCENT = "percent", "Percent"
        FIXED = "fixed", "Fixed amount per invoice"

    student = models.ForeignKey("people.Student", on_delete=models.CASCADE, related_name="concessions")
    academic_year = models.ForeignKey("academics.AcademicYear", on_delete=models.CASCADE, related_name="concessions")
    head = models.ForeignKey(FeeHead, null=True, blank=True, on_delete=models.CASCADE, related_name="concessions",
                             help_text="Leave empty to apply to every fee head")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["student__first_name", "id"]

    def clean(self):
        super().clean()
        if self.value is None:
            return
        if self.value <= 0:
            raise ValidationError({"value": "Must be more than zero."})
        if self.kind == self.Kind.PERCENT and self.value > 100:
            raise ValidationError({"value": "A percentage cannot be more than 100."})

    def __str__(self):
        unit = "%" if self.kind == self.Kind.PERCENT else " per invoice"
        return f"{self.student}: {self.value}{unit} ({self.reason})"


class FeeInvoice(TenantModel):
    """What one student owes for one billing month. Amounts are frozen once issued."""

    tenant_links = ("student", "academic_year")

    class Status(models.TextChoices):
        ISSUED = "issued", "Issued"
        CANCELLED = "cancelled", "Cancelled"

    FROZEN = ("school_id", "student_id", "academic_year_id", "month", "invoice_no", "due_date", "total")

    student = models.ForeignKey("people.Student", on_delete=models.PROTECT, related_name="invoices")
    academic_year = models.ForeignKey("academics.AcademicYear", on_delete=models.PROTECT, related_name="invoices")
    month = models.DateField(help_text="First day of the billing month")
    invoice_no = models.CharField(max_length=40)
    due_date = models.DateField()
    total = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ISSUED)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["due_date", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["school", "invoice_no"], name="uniq_invoice_no_per_school"),
            models.UniqueConstraint(
                fields=["student", "academic_year", "month"], condition=Q(status="issued"), name="one_live_invoice_per_month"
            ),
        ]
        indexes = [
            models.Index(fields=["school", "status", "due_date"], name="inv_school_status_due_idx"),
            models.Index(fields=["student", "status"], name="inv_student_status_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values(*self.FROZEN).first()
            if original is not None:
                for field in self.FROZEN:
                    if original[field] != getattr(self, field):
                        raise FeeError(f"An issued invoice cannot be changed ({field}). Cancel it and issue a new one.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.invoice_no} ({self.student})"


class FeeInvoiceLine(TenantModel):
    """One charge (positive) or discount (negative) on an invoice. Never edited after creation."""

    tenant_links = ("invoice", "head")

    class Kind(models.TextChoices):
        CHARGE = "charge", "Charge"
        DISCOUNT = "discount", "Discount"

    invoice = models.ForeignKey(FeeInvoice, on_delete=models.CASCADE, related_name="lines")
    head = models.ForeignKey(FeeHead, null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines")
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Negative for discounts")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.CHARGE)

    class Meta:
        ordering = ["pk"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise FeeError("Invoice lines cannot be edited. Cancel the invoice and issue a new one.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.description}: {self.amount}"


class Payment(TenantModel):
    """Money received from a student's family. The amount is frozen; only reversal can change the status."""

    tenant_links = ("student",)

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CHEQUE = "cheque", "Cheque"
        BANK = "bank_transfer", "Bank transfer"
        CARD = "card", "Card"
        ONLINE = "online", "Online (gateway)"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        REVERSED = "reversed", "Reversed"

    FROZEN = ("school_id", "student_id", "receipt_no", "amount", "method", "received_on", "reference")

    student = models.ForeignKey("people.Student", on_delete=models.PROTECT, related_name="payments")
    receipt_no = models.CharField(max_length=40)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=16, choices=Method.choices)
    reference = models.CharField(max_length=100, blank=True, help_text="UPI transaction id, cheque number, ...")
    received_on = models.DateField()
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    # Stops a double-click or a retried request from recording the same money twice.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reversal_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-received_on", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["school", "receipt_no"], name="uniq_receipt_no_per_school"),
            models.UniqueConstraint(
                fields=["school", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_payment_idem_key"
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
        ]
        indexes = [models.Index(fields=["school", "received_on"], name="pay_school_received_idx")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values(*self.FROZEN).first()
            if original is not None:
                for field in self.FROZEN:
                    if original[field] != getattr(self, field):
                        raise FeeError(f"A recorded payment cannot be changed ({field}). Reverse it and record a new one.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.receipt_no}: {self.amount} from {self.student}"


class PaymentAllocation(TenantModel):
    """How much of a payment went to which invoice."""

    tenant_links = ("payment", "invoice")

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey(FeeInvoice, on_delete=models.PROTECT, related_name="allocations")
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["invoice__due_date", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "invoice"], name="uniq_allocation"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="allocation_amount_positive"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise FeeError("Payment allocations cannot be edited.")
        super().save(*args, **kwargs)
