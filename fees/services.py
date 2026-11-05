"""Billing and payment rules. Views stay thin; everything that touches money lives here so it can be tested.

Per-school settings (School.settings):
    fee_due_day    day of the month invoices fall due, 1-28           default 10

Billing schedule: invoices are monthly. A fee head's frequency decides which months it appears in, counting
months from the start of the academic year (April = month 0 in an April-March session):
    monthly       every month            quarterly   months 0, 3, 6, 9
    half-yearly   months 0 and 6         annual      month 0
    one-time      the first invoice on/after the student's admission month (only for students admitted this year)
"""
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from academics.models import AcademicYear, Enrollment
from people.models import Student

from .models import (
    Concession, FeeError, FeeInvoice, FeeInvoiceLine, FeeStructure, NumberSequence, Payment, PaymentAllocation,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MONEY = DecimalField(max_digits=14, decimal_places=2)

ISSUED = FeeInvoice.Status.ISSUED
CANCELLED = FeeInvoice.Status.CANCELLED
COMPLETED = Payment.Status.COMPLETED
REVERSED = Payment.Status.REVERSED
Frequency = FeeStructure.Frequency


def today():
    """The school's 'today'. A seam: tests replace this to pin the date."""
    return timezone.localdate()


# -- amounts -----------------------------------------------------------------------------------

def money(value):
    """Round to whole paise, half-up (what people expect on a receipt)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(value):
    """Strict parsing for amounts typed by a person: positive, at most two decimals. Raises FeeError."""
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise FeeError("Enter the amount as a number, for example 2500 or 2500.50.")
    if not amount.is_finite() or amount <= 0:
        raise FeeError("The amount must be more than zero.")
    if amount != amount.quantize(CENT):
        raise FeeError("Use at most two decimal places.")
    return amount.quantize(CENT)


# -- numbering ---------------------------------------------------------------------------------

def reserve_numbers(school, key, count=1):
    """Reserve `count` consecutive numbers in a series and return the first. Row-locked, so two people
    recording payments at the same moment can never receive the same receipt number."""
    with transaction.atomic():
        sequence, _ = NumberSequence.objects.select_for_update().get_or_create(school=school, key=key)
        first = sequence.last_value + 1
        sequence.last_value += count
        sequence.save(update_fields=["last_value"])
        return first


# -- billing calendar --------------------------------------------------------------------------

def add_months(first_of_month, n):
    index = first_of_month.year * 12 + (first_of_month.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


def billing_months(year):
    first = date(year.start_date.year, year.start_date.month, 1)
    return [add_months(first, i) for i in range(12)]


def month_index(year, month):
    month = month.replace(day=1)
    index = (month.year - year.start_date.year) * 12 + month.month - year.start_date.month
    if not 0 <= index < 12:
        raise FeeError("That month is outside the academic year.")
    return index


def due_day(school):
    try:
        return min(28, max(1, int((school.settings or {}).get("fee_due_day", 10))))
    except (TypeError, ValueError):
        return 10


def _applies(frequency, index, head_id, one_time_heads):
    if frequency == Frequency.MONTHLY:
        return True
    if frequency == Frequency.QUARTERLY:
        return index % 3 == 0
    if frequency == Frequency.HALF_YEARLY:
        return index % 6 == 0
    if frequency == Frequency.ANNUAL:
        return index == 0
    if frequency == Frequency.ONE_TIME:
        return head_id in one_time_heads
    return False


def build_lines(structure_rows, index, one_time_heads, concessions):
    """The charge and discount lines for one student's invoice, as plain dicts."""
    charges = [
        {"head_id": s.head_id, "description": s.head.name, "amount": money(s.amount), "kind": FeeInvoiceLine.Kind.CHARGE}
        for s in structure_rows
        if _applies(s.frequency, index, s.head_id, one_time_heads)
    ]
    remaining = [c["amount"] for c in charges]
    discounts = []
    for concession in concessions:
        matching = [i for i, c in enumerate(charges) if concession.head_id is None or c["head_id"] == concession.head_id]
        original = sum((charges[i]["amount"] for i in matching), ZERO)
        available = sum((remaining[i] for i in matching), ZERO)
        if concession.kind == Concession.Kind.PERCENT:
            wanted = money(original * concession.value / Decimal(100))
        else:
            wanted = money(concession.value)
        amount = min(wanted, available)  # a student is never billed less than zero
        if amount <= 0:
            continue
        left = amount
        for i in matching:
            take = min(remaining[i], left)
            remaining[i] -= take
            left -= take
        discounts.append(
            {
                "head_id": concession.head_id,
                "description": f"Concession: {concession.reason}",
                "amount": -amount,
                "kind": FeeInvoiceLine.Kind.DISCOUNT,
            }
        )
    return charges + discounts


@transaction.atomic
def generate_invoices(school, year, month, user=None, *, preview=False):
    """Issue the month's invoices for every active, enrolled student. Safe to run repeatedly: students who already
    have a live invoice for the month are skipped. With preview=True nothing is written.

    Returns {'month', 'would_create', 'created', 'skipped_existing', 'skipped_no_fee', 'total'}."""
    month = month.replace(day=1)
    index = month_index(year, month)

    enrolments = list(
        Enrollment.objects.filter(school=school, academic_year=year, student__status="active")
        .order_by("student_id").values_list("student_id", "section__grade_id")
    )
    students = Student.objects.in_bulk([sid for sid, _ in enrolments])
    structures = defaultdict(list)
    for row in FeeStructure.objects.filter(school=school, academic_year=year).select_related("head").order_by("head__name"):
        structures[row.grade_id].append(row)
    concessions = defaultdict(list)
    for c in Concession.objects.filter(school=school, academic_year=year, is_active=True).order_by("pk"):
        concessions[c.student_id].append(c)
    already = set(
        FeeInvoice.objects.filter(school=school, academic_year=year, month=month, status=ISSUED).values_list("student_id", flat=True)
    )
    one_time_heads = {row.head_id for rows in structures.values() for row in rows if row.frequency == Frequency.ONE_TIME}
    billed_one_time = set(
        FeeInvoiceLine.objects.filter(
            invoice__school=school, invoice__academic_year=year, invoice__status=ISSUED, head_id__in=one_time_heads
        ).values_list("invoice__student_id", "head_id")
    )

    result = {"month": month, "would_create": 0, "created": 0, "skipped_existing": 0, "skipped_no_fee": 0, "total": ZERO}
    plan = []
    for student_id, grade_id in enrolments:
        if student_id in already:
            result["skipped_existing"] += 1
            continue
        student = students[student_id]
        eligible = set()
        admitted = student.admission_date
        if admitted and year.start_date <= admitted <= year.end_date and month >= admitted.replace(day=1):
            eligible = {h for h in one_time_heads if (student_id, h) not in billed_one_time}
        lines = build_lines(structures.get(grade_id, []), index, eligible, concessions.get(student_id, []))
        total = sum((line["amount"] for line in lines), ZERO)
        if total <= 0:
            result["skipped_no_fee"] += 1
            continue
        plan.append((student, lines, total))
        result["total"] += total
    result["would_create"] = len(plan)
    if preview or not plan:
        return result

    try:
        with transaction.atomic():
            first = reserve_numbers(school, f"invoice:{year.name}", len(plan))
            due = month.replace(day=due_day(school))
            invoices = [
                FeeInvoice(
                    school=school, student=student, academic_year=year, month=month, due_date=due, total=total,
                    invoice_no=f"INV/{year.name}/{first + i:06d}", created_by=user,
                )
                for i, (student, _lines, total) in enumerate(plan)
            ]
            FeeInvoice.objects.bulk_create(invoices)
            FeeInvoiceLine.objects.bulk_create(
                [
                    FeeInvoiceLine(
                        school=school, invoice=invoice, head_id=line["head_id"], description=line["description"],
                        amount=line["amount"], kind=line["kind"],
                    )
                    for invoice, (_student, lines, _total) in zip(invoices, plan)
                    for line in lines
                ]
            )
    except IntegrityError:
        raise FeeError("Invoices for this month were just generated by someone else. Please refresh and check.")
    result["created"] = len(plan)
    return result


# -- balances ----------------------------------------------------------------------------------

def with_balances(invoices):
    """Annotate invoices with `paid` (completed allocations) and `balance` (total - paid)."""
    paid = Coalesce(
        Sum("allocations__amount", filter=Q(allocations__payment__status=COMPLETED)), Value(ZERO), output_field=MONEY
    )
    return invoices.annotate(paid=paid).annotate(
        balance=ExpressionWrapper(F("total") - F("paid"), output_field=MONEY)
    )


def invoice_status(invoice, paid, now):
    """(code, label) for display: cancelled / paid / overdue / partial / due."""
    if invoice.status == CANCELLED:
        return "cancelled", "Cancelled"
    if invoice.total - paid <= 0:
        return "paid", "Paid"
    if invoice.due_date < now:
        return "overdue", "Overdue"
    if paid > 0:
        return "partial", "Part paid"
    return "due", "Due"


def student_account(student, now=None):
    now = now or today()
    invoices = list(
        with_balances(FeeInvoice.objects.filter(student=student)).select_related("academic_year").order_by("-month", "-pk")
    )
    rows, billed, paid, outstanding, overdue, next_due = [], ZERO, ZERO, ZERO, ZERO, None
    for invoice in invoices:
        code, label = invoice_status(invoice, invoice.paid, now)
        live = invoice.status == ISSUED
        balance = invoice.balance if live else ZERO
        rows.append({"invoice": invoice, "paid": invoice.paid, "balance": balance, "status": code, "label": label})
        if live:
            billed += invoice.total
            paid += invoice.paid
            if balance > 0:
                outstanding += balance
                if invoice.due_date < now:
                    overdue += balance
                if next_due is None or invoice.due_date < next_due:
                    next_due = invoice.due_date
    payments = list(Payment.objects.filter(student=student).order_by("-received_on", "-pk"))
    return {
        "rows": rows, "payments": payments, "billed": billed, "paid": paid,
        "outstanding": outstanding, "overdue": overdue, "next_due": next_due,
    }


def balances_for(student_ids, now=None):
    """{student_id: {'outstanding', 'overdue', 'next_due'}} for many students in one query."""
    now = now or today()
    result = {sid: {"outstanding": ZERO, "overdue": ZERO, "next_due": None} for sid in student_ids}
    rows = (
        with_balances(FeeInvoice.objects.filter(student_id__in=list(student_ids), status=ISSUED))
        .filter(balance__gt=0)
        .values_list("student_id", "due_date", "balance")
    )
    for student_id, due_date, balance in rows:
        entry = result[student_id]
        entry["outstanding"] += balance
        if due_date < now:
            entry["overdue"] += balance
        if entry["next_due"] is None or due_date < entry["next_due"]:
            entry["next_due"] = due_date
    return result


def dues_report(school, *, grade_id=None, overdue_only=False, now=None):
    """Students with money outstanding, biggest first. Each row: student, class label, outstanding, overdue, oldest_due."""
    now = now or today()
    invoices = with_balances(FeeInvoice.objects.filter(school=school, status=ISSUED)).filter(balance__gt=0)
    if overdue_only:
        invoices = invoices.filter(due_date__lt=now)
    if grade_id:
        grade_students = Enrollment.objects.filter(
            school=school, academic_year__is_current=True, section__grade_id=grade_id
        ).values("student_id")
        invoices = invoices.filter(student_id__in=grade_students)
    per_student = {}
    for student_id, due_date, balance in invoices.values_list("student_id", "due_date", "balance"):
        row = per_student.setdefault(student_id, {"outstanding": ZERO, "overdue": ZERO, "oldest_due": due_date, "invoices": 0})
        row["outstanding"] += balance
        row["invoices"] += 1
        if due_date < now:
            row["overdue"] += balance
        row["oldest_due"] = min(row["oldest_due"], due_date)

    students = Student.objects.in_bulk(list(per_student))
    classes = {
        e.student_id: f"{e.section.grade.name}-{e.section.name}"
        for e in Enrollment.objects.filter(student_id__in=list(per_student), academic_year__is_current=True)
        .select_related("section__grade")
    }
    rows = [
        {"student": students[sid], "class_label": classes.get(sid, "-"), **data} for sid, data in per_student.items()
    ]
    rows.sort(key=lambda r: (-r["outstanding"], r["student"].first_name, r["student"].last_name))
    return rows


def collection_summary(school, now=None):
    """Headline numbers for the fees overview."""
    now = now or today()
    first = now.replace(day=1)
    last = add_months(first, 1)
    total = lambda qs, field: qs.aggregate(t=Coalesce(Sum(field), Value(ZERO), output_field=MONEY))["t"]

    live = FeeInvoice.objects.filter(school=school, status=ISSUED)
    allocations = PaymentAllocation.objects.filter(school=school, invoice__status=ISSUED, payment__status=COMPLETED)
    overdue_invoices = live.filter(due_date__lt=now)
    overdue_allocations = allocations.filter(invoice__due_date__lt=now)
    return {
        "billed_this_month": total(live.filter(month=first), "total"),
        "collected_this_month": total(
            Payment.objects.filter(school=school, status=COMPLETED, received_on__gte=first, received_on__lt=last), "amount"
        ),
        "outstanding": total(live, "total") - total(allocations, "amount"),
        "overdue": total(overdue_invoices, "total") - total(overdue_allocations, "amount"),
        "students_with_dues": len(
            set(with_balances(live).filter(balance__gt=0).values_list("student_id", flat=True))
        ),
    }


# -- payments ----------------------------------------------------------------------------------

def _year_for(school, on_date):
    year = AcademicYear.objects.filter(school=school, start_date__lte=on_date, end_date__gte=on_date).first()
    year = year or AcademicYear.objects.filter(school=school, is_current=True).first()
    if year is None:
        raise FeeError("No academic year is set up for this school.")
    return year


def record_payment(school, student, amount, method, received_on, user=None, *, reference="", notes="",
                   idempotency_key=None, now=None):
    """Record money received and allocate it to the oldest dues first.

    Returns (payment, created). If `idempotency_key` was already used for this school, the earlier payment is
    returned with created=False, so a double-click or a retried request never records the money twice."""
    now = now or today()
    amount = parse_amount(amount)
    if method not in Payment.Method.values:
        raise FeeError("Choose how the payment was made.")
    if received_on > now:
        raise FeeError("The payment date can't be in the future.")
    if student.school_id != school.id:
        raise FeeError("That student does not belong to this school.")

    if idempotency_key:
        earlier = Payment.objects.filter(school=school, idempotency_key=idempotency_key).first()
        if earlier:
            if earlier.student_id != student.id or earlier.amount != amount:
                raise FeeError("This form was already used for a different payment. Please start again.")
            return earlier, False

    try:
        with transaction.atomic():
            # Lock this student's invoices so two people paying at once can't both use the same dues.
            list(FeeInvoice.objects.select_for_update().filter(student=student, status=ISSUED).values_list("pk", flat=True))
            open_invoices = list(
                with_balances(FeeInvoice.objects.filter(student=student, status=ISSUED))
                .filter(balance__gt=0).order_by("due_date", "pk")
            )
            outstanding = sum((i.balance for i in open_invoices), ZERO)
            if outstanding <= 0:
                raise FeeError("This student has no dues to pay.")
            if amount > outstanding:
                raise FeeError(f"The amount is more than the outstanding dues (Rs {outstanding}).")

            year = _year_for(school, received_on)
            number = reserve_numbers(school, f"receipt:{year.name}")
            payment = Payment.objects.create(
                school=school, student=student, receipt_no=f"RCPT/{year.name}/{number:06d}", amount=amount, method=method,
                reference=reference.strip(), received_on=received_on, received_by=user, notes=notes.strip(),
                idempotency_key=idempotency_key or None,
            )
            remaining, allocations = amount, []
            for invoice in open_invoices:
                take = min(invoice.balance, remaining)
                allocations.append(PaymentAllocation(school=school, payment=payment, invoice=invoice, amount=take))
                remaining -= take
                if remaining <= 0:
                    break
            PaymentAllocation.objects.bulk_create(allocations)
            return payment, True
    except IntegrityError:
        if idempotency_key:  # lost a race with an identical submission: hand back the winner
            earlier = Payment.objects.filter(school=school, idempotency_key=idempotency_key).first()
            if earlier:
                return earlier, False
        raise


def reverse_payment(payment, user, reason):
    """Undo a payment (bounced cheque, wrong student, ...). The record stays, marked reversed, with who and why."""
    reason = (reason or "").strip()
    if not reason:
        raise FeeError("Please give a reason for reversing this payment.")
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.status != COMPLETED:
            raise FeeError("This payment has already been reversed.")
        locked.status = REVERSED
        locked.reversed_at = timezone.now()
        locked.reversed_by = user
        locked.reversal_reason = reason[:200]
        locked.save()
    return locked


def cancel_invoice(invoice, user, reason):
    """Cancel an invoice that has had no money applied to it. Generate it again afterwards if needed."""
    reason = (reason or "").strip()
    if not reason:
        raise FeeError("Please give a reason for cancelling this invoice.")
    with transaction.atomic():
        locked = FeeInvoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.status != ISSUED:
            raise FeeError("This invoice is already cancelled.")
        paid = locked.allocations.filter(payment__status=COMPLETED).aggregate(t=Coalesce(Sum("amount"), Value(ZERO), output_field=MONEY))["t"]
        if paid > 0:
            raise FeeError("Money has already been paid against this invoice. Reverse those payments first.")
        locked.status = CANCELLED
        locked.cancelled_at = timezone.now()
        locked.cancelled_by = user
        locked.cancel_reason = reason[:200]
        locked.save()
    return locked
