import csv
import uuid
from collections import OrderedDict
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from academics.models import AcademicYear, Enrollment, Grade
from academics.queries import with_current_class
from accounts.access import has_perm, scopes_for, students_in_scope, visible_students
from accounts.decorators import require_perm
from accounts.permissions import SCOPE_ALL, SCOPE_CHILDREN, SCOPE_SELF
from people.models import Student

from . import services
from .models import FeeError, FeeInvoice, FeeStructure, Payment

ZERO = Decimal("0.00")

# "Online (gateway)" is reserved for payments that arrive from the payment gateway, so staff can't mislabel a typed-in one.
MANUAL_METHODS = [(code, label) for code, label in Payment.Method.choices if code != Payment.Method.ONLINE]


def _current_year(school):
    return AcademicYear.objects.filter(school=school, is_current=True).first()


def _parse_date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _csv_safe(value):
    """Stop spreadsheet apps from running a name like '=HYPERLINK(...)' as a formula."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# -- everyone with fees.view -------------------------------------------------------------------

@require_perm("fees.view")
def home(request):
    school, user = request.school, request.user
    now = services.today()
    scopes = scopes_for(user, school, "fees.view")
    ctx = {"now": now}

    if SCOPE_ALL in scopes:
        ctx["summary"] = services.collection_summary(school, now)
        ctx["can_collect"] = has_perm(user, school, "fees.collect", all_scope=True)
        ctx["can_manage"] = has_perm(user, school, "fees.manage", all_scope=True)
        ctx["recent_payments"] = Payment.objects.filter(school=school).select_related("student").order_by("-created_at", "-pk")[:8]

    kid_scopes = scopes & {SCOPE_CHILDREN, SCOPE_SELF}
    if kid_scopes:
        ids = set()
        for scope in kid_scopes:
            ids |= set(students_in_scope(user, school, scope).values_list("pk", flat=True))
        kids = list(with_current_class(Student.objects.filter(pk__in=ids)).order_by("first_name"))
        balances = services.balances_for(ids, now)
        for kid in kids:
            kid.fee = balances[kid.pk]
        ctx["kids"] = kids
    return render(request, "fees/home.html", ctx)


@require_perm("fees.view")
def student_account(request, pk):
    school, user = request.school, request.user
    student = get_object_or_404(with_current_class(visible_students(user, school, "fees.view")), pk=pk)
    return render(
        request,
        "fees/student.html",
        {
            "student": student,
            "account": services.student_account(student),
            "can_collect": has_perm(user, school, "fees.collect", all_scope=True),
        },
    )


@require_perm("fees.view")
def invoice_detail(request, pk):
    school, user = request.school, request.user
    visible = visible_students(user, school, "fees.view")
    invoice = get_object_or_404(
        FeeInvoice.objects.filter(school=school, student__in=visible.values("pk")).select_related("student", "academic_year"), pk=pk
    )
    allocations = list(invoice.allocations.select_related("payment").order_by("payment__received_on", "pk"))
    paid = sum((a.amount for a in allocations if a.payment.status == Payment.Status.COMPLETED), ZERO)
    now = services.today()
    code, label = services.invoice_status(invoice, paid, now)
    student = with_current_class(Student.objects.filter(pk=invoice.student_id)).get()
    return render(
        request,
        "fees/invoice.html",
        {
            "invoice": invoice, "student": student, "lines": list(invoice.lines.all()), "allocations": allocations,
            "paid": paid, "balance": invoice.total - paid if invoice.status == FeeInvoice.Status.ISSUED else ZERO,
            "status_code": code, "status_label": label,
            "can_cancel": has_perm(user, school, "fees.manage", all_scope=True)
            and invoice.status == FeeInvoice.Status.ISSUED and paid == 0,
        },
    )


@require_perm("fees.view")
def receipt(request, pk):
    school, user = request.school, request.user
    visible = visible_students(user, school, "fees.view")
    payment = get_object_or_404(
        Payment.objects.filter(school=school, student__in=visible.values("pk")).select_related("student", "received_by", "reversed_by"),
        pk=pk,
    )
    student = with_current_class(Student.objects.filter(pk=payment.student_id)).get()
    return render(
        request,
        "fees/receipt.html",
        {
            "payment": payment, "student": student,
            "allocations": list(payment.allocations.select_related("invoice")),
            "can_reverse": has_perm(user, school, "fees.manage", all_scope=True) and payment.status == Payment.Status.COMPLETED,
        },
    )


@require_perm("fees.view")
def fee_structure(request):
    school, user = request.school, request.user
    year = _current_year(school)

    if SCOPE_ALL in scopes_for(user, school, "fees.view"):
        grades = Grade.objects.filter(school=school)
    else:
        visible = visible_students(user, school, "fees.view")
        grade_ids = Enrollment.objects.filter(
            student__in=visible.values("pk"), academic_year__is_current=True
        ).values("section__grade_id")
        grades = Grade.objects.filter(school=school, pk__in=grade_ids)

    groups = OrderedDict()
    if year:
        rows = FeeStructure.objects.filter(school=school, academic_year=year, grade__in=grades).select_related("grade", "head")
        for row in rows:
            group = groups.setdefault(row.grade_id, {"grade": row.grade, "rows": [], "yearly_total": ZERO})
            group["rows"].append(row)
            group["yearly_total"] += row.yearly_amount

    return render(request, "web/fee_structure.html", {"year": year, "groups": list(groups.values())})


# -- staff: whole-school money actions ---------------------------------------------------------

@require_perm("fees.collect", all_scope=True)
def collect(request, student_pk=None):
    school, user = request.school, request.user
    now = services.today()

    if student_pk is None:  # step 1: find the student
        q = request.GET.get("q", "").strip()
        results = []
        if q:
            found = (
                with_current_class(Student.objects.filter(school=school, status=Student.Status.ACTIVE))
                .filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(admission_no__icontains=q))
                .order_by("first_name", "last_name")[:20]
            )
            results = list(found)
            balances = services.balances_for([s.pk for s in results], now)
            for student in results:
                student.fee = balances[student.pk]
        return render(request, "fees/collect_search.html", {"q": q, "results": results})

    student = get_object_or_404(with_current_class(Student.objects.filter(school=school)), pk=student_pk)
    account = services.student_account(student, now)
    form = {
        "amount": "", "method": "cash", "reference": "", "notes": "", "received_on": now.isoformat(),
        "token": uuid.uuid4().hex,
    }

    if request.method == "POST":
        form.update({k: request.POST.get(k, "").strip() for k in ("amount", "method", "reference", "notes", "token")})
        received_on = _parse_date(request.POST.get("received_on"), None)
        form["received_on"] = request.POST.get("received_on", "")
        try:
            if received_on is None:
                raise FeeError("Enter a valid payment date.")
            if form["method"] not in dict(MANUAL_METHODS):
                raise FeeError("Choose how the payment was made.")
            payment, created = services.record_payment(
                school, student, form["amount"], form["method"], received_on, user,
                reference=form["reference"], notes=form["notes"], idempotency_key=form["token"] or None, now=now,
            )
        except FeeError as error:
            messages.error(request, str(error))
        else:
            if created:
                messages.success(request, f"Payment recorded. Receipt {payment.receipt_no}.")
            else:
                messages.info(request, f"This payment was already recorded earlier. Receipt {payment.receipt_no}.")
            return redirect("fee_receipt", pk=payment.pk)

    open_rows = sorted((r for r in account["rows"] if r["balance"] > 0), key=lambda r: (r["invoice"].due_date, r["invoice"].pk))
    return render(
        request,
        "fees/collect.html",
        {"student": student, "account": account, "open_rows": open_rows, "form": form, "methods": MANUAL_METHODS, "now": now},
    )


def _billable_months(year, now):
    return [m for m in services.billing_months(year) if m <= now.replace(day=1)]


@require_perm("fees.manage", all_scope=True)
def generate(request):
    school, user = request.school, request.user
    now = services.today()
    year = _current_year(school)
    if year is None:
        return render(request, "fees/generate.html", {"year": None})

    months = _billable_months(year, now)
    requested = None
    raw = request.POST.get("month") or request.GET.get("month") or ""
    try:
        y, m = (int(part) for part in raw.split("-"))
        requested = next((mo for mo in months if (mo.year, mo.month) == (y, m)), None)
    except ValueError:
        pass
    chosen = requested or (months[-1] if months else year.start_date.replace(day=1))

    if request.method == "POST":
        if requested is None:  # never guess which month to bill
            messages.error(request, "Please choose one of the listed months to bill.")
            return redirect("fee_generate")
        try:
            result = services.generate_invoices(school, year, chosen, user)
        except FeeError as error:
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                f"Created {result['created']} invoice{'s' if result['created'] != 1 else ''} for {chosen:%B %Y} "
                f"({result['skipped_existing']} already existed, {result['skipped_no_fee']} had nothing to bill).",
            )
        return redirect(f"{reverse('fee_generate')}?month={chosen:%Y-%m}")

    try:
        preview = services.generate_invoices(school, year, chosen, preview=True)
    except FeeError as error:
        messages.error(request, str(error))
        preview = None
    return render(
        request, "fees/generate.html", {"year": year, "months": months, "chosen": chosen, "preview": preview}
    )


@require_perm("fees.view", all_scope=True)
def dues(request):
    school = request.school
    now = services.today()
    grade_id = request.GET.get("grade", "")
    grade_id = int(grade_id) if grade_id.isdigit() else None
    overdue_only = request.GET.get("overdue") == "1"
    rows = services.dues_report(school, grade_id=grade_id, overdue_only=overdue_only, now=now)

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="fee-dues-{now.isoformat()}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Admission no", "Student", "Class", "Outstanding (INR)", "Overdue (INR)", "Oldest due date", "Open invoices"])
        for row in rows:
            writer.writerow(
                [
                    _csv_safe(row["student"].admission_no), _csv_safe(row["student"].full_name), _csv_safe(row["class_label"]),
                    row["outstanding"], row["overdue"], row["oldest_due"].isoformat(), row["invoices"],
                ]
            )
        return response

    return render(
        request,
        "fees/dues.html",
        {
            "rows": rows, "now": now, "grades": Grade.objects.filter(school=school), "grade_id": grade_id,
            "overdue_only": overdue_only,
            "can_remind": has_perm(request.user, school, "fees.collect", all_scope=True),
            "total_outstanding": sum((r["outstanding"] for r in rows), ZERO),
            "total_overdue": sum((r["overdue"] for r in rows), ZERO),
        },
    )


@require_perm("fees.manage", all_scope=True)
@require_POST
def reverse_payment_view(request, pk):
    payment = get_object_or_404(Payment.objects.filter(school=request.school), pk=pk)
    try:
        services.reverse_payment(payment, request.user, request.POST.get("reason", ""))
    except FeeError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Payment {payment.receipt_no} was reversed. The dues are open again.")
    return redirect("fee_receipt", pk=payment.pk)


@require_perm("fees.manage", all_scope=True)
@require_POST
def cancel_invoice_view(request, pk):
    invoice = get_object_or_404(FeeInvoice.objects.filter(school=request.school), pk=pk)
    try:
        services.cancel_invoice(invoice, request.user, request.POST.get("reason", ""))
    except FeeError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Invoice {invoice.invoice_no} was cancelled.")
    return redirect("fee_invoice", pk=invoice.pk)
