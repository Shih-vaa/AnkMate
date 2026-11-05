import calendar
from datetime import date, timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from academics.models import AcademicYear, Enrollment, Section
from academics.queries import with_current_class
from accounts.access import scopes_for, students_in_scope, visible_sections, visible_students
from accounts.decorators import require_any_perm, require_perm
from accounts.permissions import SCOPE_CHILDREN, SCOPE_SELF
from people.models import Student

from . import services
from .models import AttendanceRecord, Status

STATUS_CHOICES = list(Status.choices)


def _current_year(school):
    return AcademicYear.objects.filter(school=school, is_current=True).first()


def _parse_date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


@require_any_perm("attendance.view", "attendance.mark")
def home(request):
    school, user = request.school, request.user
    now = services.today()
    year = _current_year(school)

    viewable = set(visible_sections(user, school, "attendance.view").values_list("pk", flat=True))
    markable = set(services.markable_sections(user, school).values_list("pk", flat=True))
    section_ids = viewable | markable

    today_reason = services.non_working_reason(school, now)
    overview = services.day_overview(section_ids, now)
    class_rows = []
    for section in Section.objects.filter(pk__in=section_ids).select_related("grade").order_by("grade__order", "name"):
        stats = overview[section.pk]
        if today_reason:
            state = "closed"
        elif stats["marked"]:
            state = "marked"
        else:
            state = "not_marked"
        class_rows.append(
            {"section": section, "can_mark": section.pk in markable, "can_view": section.pk in viewable, "state": state, **stats}
        )

    kid_scopes = scopes_for(user, school, "attendance.view") & {SCOPE_CHILDREN, SCOPE_SELF}
    kids = []
    if kid_scopes:
        ids = set()
        for scope in kid_scopes:
            ids |= set(students_in_scope(user, school, scope).values_list("pk", flat=True))
        kids = list(with_current_class(Student.objects.filter(pk__in=ids)).order_by("first_name"))
        month_start, month_end = services.month_range(now.year, now.month)
        this_month = services.percentages_for(ids, month_start, month_end)
        year_to_date = services.percentages_for(ids, year.start_date if year else month_start, now)
        for kid in kids:
            kid.att_month, kid.att_year = this_month[kid.pk], year_to_date[kid.pk]

    return render(
        request,
        "attendance/home.html",
        {"today": now, "today_reason": today_reason, "class_rows": class_rows, "kids": kids},
    )


@require_perm("attendance.mark")
def mark(request, section_pk):
    school, user = request.school, request.user
    # Looked up through the "may mark" queryset: a class that isn't yours is simply not found.
    section = get_object_or_404(
        services.markable_sections(user, school).select_related("grade", "academic_year", "class_teacher"), pk=section_pk
    )
    now = services.today()
    on_date = _parse_date(request.POST.get("date") or request.GET.get("date"), now)

    allowed, why_not = services.can_mark(user, school, section, on_date, now=now)
    closed_reason = services.non_working_reason(school, on_date)
    editable = allowed and not closed_reason
    students = services.roster(section)
    here = f"{reverse('attendance_mark', args=[section.pk])}?date={on_date.isoformat()}"

    posted = {}
    if request.method == "POST":
        if not editable:
            messages.error(request, closed_reason or why_not)
            return redirect(here)
        posted = {e.student_id: request.POST.get(f"status_{e.student_id}") for e in students}
        if any(status not in Status.values for status in posted.values()):
            messages.error(request, "Please choose a status for every student, then save again.")
        else:
            services.save_section_attendance(section, on_date, posted, user)
            counts = services.summarize(posted.values())["counts"]
            messages.success(
                request,
                f"Saved {section} for {on_date:%a %d %b}: {counts['P'] + counts['L'] + counts['H']} present, "
                f"{counts['A'] + counts['E']} absent or on leave.",
            )
            return redirect(here)

    existing = dict(
        AttendanceRecord.objects.filter(student_id__in=[e.student_id for e in students], date=on_date)
        .values_list("student_id", "status")
    )
    already_marked = bool(existing)
    rows = [
        {
            "student": e.student,
            "roll_no": e.roll_no,
            # Fresh day: everyone starts as Present and the teacher changes the exceptions.
            "current": posted.get(e.student_id) or existing.get(e.student_id) or Status.PRESENT.value,
        }
        for e in students
    ]
    return render(
        request,
        "attendance/mark.html",
        {
            "section": section, "rows": rows, "statuses": STATUS_CHOICES, "today": now, "on_date": on_date,
            "prev_date": on_date - timedelta(days=1), "next_date": on_date + timedelta(days=1) if on_date < now else None,
            "editable": editable, "blocked_reason": closed_reason or (why_not if not allowed else ""),
            "closed": bool(closed_reason), "already_marked": already_marked,
            "window": services.rules_for(school).edit_window_days,
        },
    )


@require_perm("attendance.view")
def student_attendance(request, pk):
    school, user = request.school, request.user
    student = get_object_or_404(with_current_class(visible_students(user, school, "attendance.view")), pk=pk)
    now = services.today()
    year = _current_year(school)

    (y, m), prev_key, next_key = services.resolve_month(request.GET.get("month"), year, now)
    first_day, last_day = services.month_range(y, m)
    records = {
        r.date: r
        for r in AttendanceRecord.objects.filter(student=student, date__range=(first_day, last_day))
    }
    holidays = services.holidays_between(school, first_day, last_day)
    rules = services.rules_for(school)

    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(y, m):
        cells = []
        for d in week:
            cell = {"date": d, "in_month": d.month == m, "status": None, "note": ""}
            if cell["in_month"]:
                record = records.get(d)
                reason = services.non_working_reason(school, d, rules=rules, holidays=holidays)
                if record:
                    cell["status"] = record.status
                elif reason:
                    cell["note"] = reason
                    cell["off"] = True
                elif d > now:
                    cell["future"] = True
                else:
                    cell["unmarked"] = True
            cells.append(cell)
        weeks.append(cells)

    exceptions = [r for r in sorted(records.values(), key=lambda r: r.date) if r.status != Status.PRESENT]
    year_start = year.start_date if year else first_day
    return render(
        request,
        "attendance/student.html",
        {
            "student": student, "weeks": weeks, "month_start": first_day, "prev_key": prev_key, "next_key": next_key,
            "month": services.summarize(r.status for r in records.values()),
            "year_to_date": services.percentages_for([student.pk], year_start, now)[student.pk],
            "exceptions": exceptions, "statuses": STATUS_CHOICES, "holiday_list": sorted(holidays.items()),
        },
    )


@require_perm("attendance.view")
def register(request, section_pk):
    school, user = request.school, request.user
    section = get_object_or_404(
        visible_sections(user, school, "attendance.view").select_related("grade", "class_teacher"), pk=section_pk
    )
    now = services.today()
    year = _current_year(school)
    (y, m), prev_key, next_key = services.resolve_month(request.GET.get("month"), year, now)
    first_day, last_day = services.month_range(y, m)

    enrolments = list(
        Enrollment.objects.filter(section=section).select_related("student").order_by("roll_no", "student__first_name")
    )
    records = {
        (r["student_id"], r["date"]): r["status"]
        for r in AttendanceRecord.objects.filter(student_id__in=[e.student_id for e in enrolments], date__range=(first_day, last_day))
        .values("student_id", "date", "status")
    }
    holidays = services.holidays_between(school, first_day, last_day)
    rules = services.rules_for(school)
    days = []
    for d in services.daterange(first_day, last_day):
        days.append({"date": d, "off": services.non_working_reason(school, d, rules=rules, holidays=holidays), "present": 0})

    rows = []
    for e in enrolments:
        cells, statuses = [], []
        for day in days:
            status = records.get((e.student_id, day["date"]))
            cells.append(status)
            if status:
                statuses.append(status)
                if status in services.PRESENT_LIKE:
                    day["present"] += 1
        rows.append({"student": e.student, "roll_no": e.roll_no, "cells": cells, "summary": services.summarize(statuses)})

    return render(
        request,
        "attendance/register.html",
        {
            "section": section, "days": days, "rows": rows, "month_start": first_day,
            "prev_key": prev_key, "next_key": next_key, "statuses": STATUS_CHOICES,
            "can_mark": services.markable_sections(user, school).filter(pk=section.pk).exists(),
        },
    )
