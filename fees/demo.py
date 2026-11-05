"""Fake fee history for the demo school (never run against real data).

Creates concessions, a monthly invoice run from the start of the session to the current month, and payments
that follow realistic habits: most families pay on time, some pay late, a few have fallen behind."""
import random
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from academics.models import AcademicYear, Enrollment
from people.models import StudentGuardian

from . import services
from .models import (
    Concession, FeeHead, FeeInvoice, FeeInvoiceLine, NumberSequence, Payment, PaymentAllocation,
)

METHODS = ["cash"] * 8 + ["upi"] * 9 + ["cheque", "bank_transfer", "card"]


def clear_fees(school):
    """Delete the school's whole fee ledger (demo data only)."""
    PaymentAllocation.objects.filter(school=school).delete()
    Payment.objects.filter(school=school).delete()
    FeeInvoiceLine.objects.filter(school=school).delete()
    FeeInvoice.objects.filter(school=school).delete()
    Concession.objects.filter(school=school).delete()
    NumberSequence.objects.filter(school=school).delete()


def seed_fees(school, *, seed=11, reset=False, now=None):
    """Returns {'invoices': n, 'payments': n, 'concessions': n}."""
    now = now or services.today()
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    if year is None:
        raise ValueError("The school has no current academic year.")
    if FeeInvoice.objects.filter(school=school).exists():
        if not reset:
            raise ValueError("Fee invoices already exist. Pass reset=True to replace them.")
        clear_fees(school)

    rng = random.Random(seed)
    enrolments = list(
        Enrollment.objects.filter(school=school, academic_year=year, student__status="active")
        .select_related("section__grade", "student").order_by("section__grade__order", "roll_no")
    )

    # -- concessions: younger siblings get 10% off tuition; a few scholarships --------------------------
    tuition = FeeHead.objects.filter(school=school, code="TUI").first()
    by_guardian = defaultdict(list)
    for link in StudentGuardian.objects.filter(school=school).select_related("student"):
        by_guardian[link.guardian_id].append(link.student_id)
    grade_order = {e.student_id: e.section.grade.order for e in enrolments}
    sibling_ids = set()
    for student_ids in by_guardian.values():
        in_school = sorted((sid for sid in set(student_ids) if sid in grade_order), key=lambda sid: grade_order[sid])
        sibling_ids.update(in_school[1:])  # the eldest pays full tuition
    concessions = [
        Concession(school=school, student_id=sid, academic_year=year, head=tuition, kind="percent", value=Decimal("10"),
                   reason="Sibling concession")
        for sid in sorted(sibling_ids)
    ]
    others = [e.student_id for e in enrolments if e.student_id not in sibling_ids]
    rng.shuffle(others)
    concessions += [
        Concession(school=school, student_id=sid, academic_year=year, kind="fixed", value=Decimal("300"), reason="Merit scholarship")
        for sid in others[:4]
    ]
    concessions.append(
        Concession(school=school, student_id=others[4], academic_year=year, kind="percent", value=Decimal("50"), reason="Staff child")
    )
    Concession.objects.bulk_create(concessions)

    # -- one invoice run per month so far ----------------------------------------------------------------
    months = [m for m in services.billing_months(year) if m <= now.replace(day=1)]
    invoices_made = 0
    for month in months:
        invoices_made += services.generate_invoices(school, year, month)["created"]

    # -- payments, in date order so receipt numbers rise with time ----------------------------------------
    invoices = FeeInvoice.objects.filter(school=school, status="issued").order_by("student_id", "month")
    per_student = defaultdict(list)
    for invoice in invoices:
        per_student[invoice.student_id].append(invoice)
    student_obj = {e.student_id: e.student for e in enrolments}

    planned = []  # (date, student_id, amount)
    for student_id, student_invoices in per_student.items():
        habit = rng.random()
        if habit < 0.72:
            last_paid_month = months[-1]       # keeps up, even pays the current month sometimes
        elif habit < 0.86:
            last_paid_month = months[max(0, len(months) - 3)]   # a couple of months behind
        else:
            last_paid_month = months[max(0, len(months) - 5)]   # well behind
        for invoice in student_invoices:
            if invoice.month > last_paid_month:
                continue
            if invoice.month == months[-1] and rng.random() < 0.6:
                continue  # this month's invoice is mostly still unpaid
            if habit < 0.72:
                paid_on = invoice.due_date - timedelta(days=rng.randint(0, 8))  # on time, a few days early
            else:  # late payers settle some time after the due date, but never in the future
                latest = min(invoice.due_date + timedelta(days=20), now)
                if latest < invoice.due_date:
                    continue  # not even due yet
                paid_on = invoice.due_date + timedelta(days=rng.randint(0, (latest - invoice.due_date).days))
            paid_on = min(max(paid_on, invoice.month), now)
            planned.append((paid_on, student_id, invoice.total))
        if rng.random() < 0.05 and student_invoices:  # a part payment toward the oldest unpaid invoice
            unpaid = [i for i in student_invoices if i.month > last_paid_month]
            if unpaid:
                planned.append((min(unpaid[0].due_date, now), student_id, (unpaid[0].total * Decimal("0.5")).quantize(Decimal("0.01"))))

    planned.sort(key=lambda row: (row[0], row[1]))
    payments_made = 0
    for received_on, student_id, amount in planned:
        method = rng.choice(METHODS)
        reference = {"upi": f"UPI{rng.randint(10**9, 10**10 - 1)}", "cheque": f"CHQ{rng.randint(100000, 999999)}",
                     "bank_transfer": f"NEFT{rng.randint(10**9, 10**10 - 1)}"}.get(method, "")
        try:
            services.record_payment(school, student_obj[student_id], amount, method, received_on, None, reference=reference, now=now)
            payments_made += 1
        except services.FeeError:
            continue  # nothing left to pay for that student: fine for demo data
    return {"invoices": invoices_made, "payments": payments_made, "concessions": len(concessions)}
