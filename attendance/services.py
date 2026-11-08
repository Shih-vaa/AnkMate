"""Attendance rules and calculations.

Views stay thin: every rule (who may mark, which days are open, how percentages work) lives here
so it can be tested without a browser. Per-school behaviour comes from `School.settings`:

    weekly_off                    list of weekday numbers, Monday=0 ... Sunday=6      default [6]
    attendance_marked_by          "class_teacher" | "assigned_teachers"               default "class_teacher"
    attendance_edit_window_days   how far back a teacher may mark/edit                default 7
"""
import calendar
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from academics.models import Enrollment, Section
from accounts.access import memberships_for
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED
from people.models import Staff

from .models import AttendanceRecord, Holiday, Status
from .signals import attendance_saved

logger = logging.getLogger(__name__)

PRESENT_LIKE = [Status.PRESENT.value, Status.LATE.value, Status.HALF_DAY.value]

# How much of a day each status counts towards the attendance percentage.
# "On leave" counts as not attended (it is shown separately so nobody is blamed for approved leave).
WEIGHT = {
    Status.PRESENT.value: 1.0,
    Status.LATE.value: 1.0,
    Status.HALF_DAY.value: 0.5,
    Status.ABSENT.value: 0.0,
    Status.LEAVE.value: 0.0,
}

MARKED_BY_CHOICES = ("class_teacher", "assigned_teachers")


def today():
    """The school's 'today'. A seam: tests replace this to pin the date."""
    return timezone.localdate()


class AttendanceError(ValueError):
    """Bad input to a save (unknown student, unknown status, ...)."""


# -- school rules ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rules:
    weekly_off: frozenset
    marked_by: str
    edit_window_days: int


def rules_for(school):
    cfg = school.settings or {}
    weekly_off = set()
    for value in cfg.get("weekly_off", [6]):
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 6:
            weekly_off.add(number)
    marked_by = cfg.get("attendance_marked_by")
    if marked_by not in MARKED_BY_CHOICES:
        marked_by = "class_teacher"
    try:
        window = max(0, int(cfg.get("attendance_edit_window_days", 7)))
    except (TypeError, ValueError):
        window = 7
    return Rules(frozenset(weekly_off), marked_by, window)


def holidays_between(school, start, end):
    return dict(Holiday.objects.filter(school=school, date__range=(start, end)).values_list("date", "name"))


def non_working_reason(school, on_date, *, rules=None, holidays=None):
    """None for a normal school day, otherwise the reason it is closed (holiday name or 'Weekly off')."""
    rules = rules or rules_for(school)
    if holidays is None:
        holidays = holidays_between(school, on_date, on_date)
    if on_date in holidays:
        return holidays[on_date]
    if on_date.weekday() in rules.weekly_off:
        return "Weekly off"
    return None


def daterange(start, end):
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


# -- who may mark ------------------------------------------------------------------------------

def _mark_memberships(user, school):
    return [m for m in memberships_for(user, school) if "attendance.mark" in m.role.permissions]


def markable_sections(user, school):
    """Current-year sections this user is allowed to take attendance for."""
    base = Section.objects.filter(school=school, academic_year__is_current=True)
    rules = rules_for(school)
    combined = Q()
    found = False
    for membership in _mark_memberships(user, school):
        if membership.role.scope == SCOPE_ALL:
            return base
        if membership.role.scope == SCOPE_ASSIGNED:
            staff = Staff.objects.filter(school=school, user=user, is_active=True).values("pk")
            combined |= Q(class_teacher__in=staff)
            if rules.marked_by == "assigned_teachers":
                combined |= Q(assignments__teacher__in=staff)
            found = True
    return base.filter(combined).distinct() if found else base.none()


def can_mark(user, school, section, on_date, *, now=None):
    """(allowed, reason). `reason` is a message fit to show the user when not allowed."""
    now = now or today()
    memberships = _mark_memberships(user, school)
    if not memberships:
        return False, "You don't have permission to take attendance."
    if not markable_sections(user, school).filter(pk=section.pk).exists():
        return False, "Only this class's teacher can take its attendance."
    year = section.academic_year
    if on_date > now:
        return False, "Attendance can't be marked for a future date."
    if on_date < year.start_date or on_date > year.end_date:
        return False, "That date is outside the academic year."
    if not any(m.role.scope == SCOPE_ALL for m in memberships):
        window = rules_for(school).edit_window_days
        if (now - on_date).days > window:
            return False, f"Teachers can change attendance only up to {window} days back. Please ask the school office."
    return True, ""


# -- marking -----------------------------------------------------------------------------------

def roster(section):
    """Active students enrolled in the section, in roll-number order."""
    return list(
        Enrollment.objects.filter(section=section, student__status="active")
        .select_related("student")
        .order_by("roll_no", "student__first_name")
    )


@transaction.atomic
def save_section_attendance(section, on_date, marks, user):
    """Create or replace attendance for `marks` = {student_id: status}. Raises AttendanceError on bad input."""
    if not marks:
        raise AttendanceError("Nothing to save.")
    allowed = {e.student_id for e in roster(section)}
    unknown = set(marks) - allowed
    if unknown:
        raise AttendanceError("Some students are not in this class.")
    valid = set(Status.values)
    if any(status not in valid for status in marks.values()):
        raise AttendanceError("Unknown attendance status.")

    now = timezone.now()
    AttendanceRecord.objects.bulk_create(
        [
            AttendanceRecord(
                school_id=section.school_id, student_id=sid, section=section, date=on_date,
                status=status, marked_by=user, marked_at=now,
            )
            for sid, status in marks.items()
        ],
        update_conflicts=True,
        unique_fields=["student", "date"],
        update_fields=["status", "section", "marked_by", "marked_at"],
    )
    # Listeners (for example absence alerts to parents) must never be able to stop attendance from being saved:
    # send_robust catches their errors, and the listeners do their own work inside a savepoint.
    for receiver, outcome in attendance_saved.send_robust(
        sender=None, section=section, on_date=on_date, marks=dict(marks), user=user
    ):
        if isinstance(outcome, Exception):
            logger.error("An attendance listener failed: %r", outcome, exc_info=outcome)
    return len(marks)


# -- summaries ---------------------------------------------------------------------------------

def summarize_counts(counts):
    counts = Counter(counts)
    marked = sum(counts.values())
    attended = sum(WEIGHT[code] * n for code, n in counts.items())
    return {
        "counts": {code: counts.get(code, 0) for code in Status.values},
        "marked": marked,
        "attended": attended,
        "percentage": round(attended / marked * 100, 1) if marked else None,
    }


def summarize(statuses):
    return summarize_counts(Counter(statuses))


def percentages_for(student_ids, start, end):
    """{student_id: summary} for many students in one query."""
    student_ids = list(student_ids)
    rows = (
        AttendanceRecord.objects.filter(student_id__in=student_ids, date__range=(start, end))
        .order_by()
        .values("student_id", "status")
        .annotate(n=Count("id"))
    )
    by_student = defaultdict(Counter)
    for row in rows:
        by_student[row["student_id"]][row["status"]] += row["n"]
    return {sid: summarize_counts(by_student.get(sid, Counter())) for sid in student_ids}


def day_overview(section_ids, on_date):
    """{section_id: {'total', 'marked', 'present'}} for one day across many sections."""
    section_ids = list(section_ids)
    totals = dict(
        Enrollment.objects.filter(section_id__in=section_ids, student__status="active")
        .order_by()
        .values_list("section_id")
        .annotate(n=Count("id"))
    )
    stats = {sid: {"total": totals.get(sid, 0), "marked": 0, "present": 0} for sid in section_ids}
    rows = (
        AttendanceRecord.objects.filter(section_id__in=section_ids, date=on_date)
        .order_by()
        .values("section_id")
        .annotate(marked=Count("id"), present=Count("id", filter=Q(status__in=PRESENT_LIKE)))
    )
    for row in rows:
        stats[row["section_id"]].update(marked=row["marked"], present=row["present"])
    return stats


def school_day_summary(school, on_date):
    ids = list(Section.objects.filter(school=school, academic_year__is_current=True).values_list("pk", flat=True))
    stats = day_overview(ids, on_date)
    return {
        "total": sum(v["total"] for v in stats.values()),
        "marked": sum(v["marked"] for v in stats.values()),
        "present": sum(v["present"] for v in stats.values()),
        "sections": len(stats),
        "sections_marked": sum(1 for v in stats.values() if v["marked"]),
        "reason": non_working_reason(school, on_date),
    }


# -- month helpers -----------------------------------------------------------------------------

def parse_month(value, default):
    """'2026-11' -> (2026, 11); anything unusable -> default."""
    try:
        year_s, month_s = value.split("-")
        year, month = int(year_s), int(month_s)
        date(year, month, 1)
        return year, month
    except (AttributeError, ValueError):
        return default


def month_range(year, month):
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def shift_month(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_key(year, month):
    return f"{year:04d}-{month:02d}"


def month_bounds(academic_year, now):
    """(first, last) selectable months: from the year's first month to the current month."""
    last = (now.year, now.month)
    if academic_year is None:
        return last, last
    first = (academic_year.start_date.year, academic_year.start_date.month)
    return first, max(first, min(last, (academic_year.end_date.year, academic_year.end_date.month)))


def resolve_month(requested, academic_year, now):
    """Parse ?month= and keep it inside the selectable range. Returns ((y, m), prev_key, next_key)."""
    first, last = month_bounds(academic_year, now)
    chosen = parse_month(requested, last)
    if not first <= chosen <= last:
        chosen = last
    prev_ym, next_ym = shift_month(*chosen, -1), shift_month(*chosen, 1)
    return (
        chosen,
        month_key(*prev_ym) if prev_ym >= first else None,
        month_key(*next_ym) if next_ym <= last else None,
    )
