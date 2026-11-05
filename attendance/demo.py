"""Fake attendance history for the demo school (never run against real data)."""
import random
from datetime import date, timedelta

from academics.models import AcademicYear, Enrollment, Section

from . import services
from .models import AttendanceRecord, Holiday, Status

# Fixed-date national holidays plus Christmas (a convent school). Sample data, not the school's real calendar.
SAMPLE_HOLIDAYS = [
    (8, 15, "Independence Day"),
    (10, 2, "Gandhi Jayanti"),
    (12, 25, "Christmas"),
    (1, 26, "Republic Day"),
]


def seed_attendance(school, *, seed=7, reset=False, now=None):
    """Create attendance from the start of the academic year up to yesterday, and mark about 60% of
    classes for today so the dashboards show both 'taken' and 'not taken'. Returns the number of records."""
    now = now or services.today()
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    if year is None:
        raise ValueError("The school has no current academic year.")
    if AttendanceRecord.objects.filter(school=school).exists():
        if not reset:
            raise ValueError("Attendance already exists. Pass reset=True to replace it.")
        AttendanceRecord.objects.filter(school=school).delete()

    for month, day, name in SAMPLE_HOLIDAYS:
        holiday_year = year.start_date.year if month >= year.start_date.month else year.start_date.year + 1
        Holiday.objects.get_or_create(school=school, date=date(holiday_year, month, day), defaults={"name": name})

    rng = random.Random(seed)
    rules = services.rules_for(school)
    holidays = services.holidays_between(school, year.start_date, year.end_date)

    def open_days(start, end):
        return [
            d for d in services.daterange(start, end)
            if services.non_working_reason(school, d, rules=rules, holidays=holidays) is None
        ]

    past_days = open_days(year.start_date, min(now - timedelta(days=1), year.end_date))
    today_is_open = year.start_date <= now <= year.end_date and services.non_working_reason(
        school, now, rules=rules, holidays=holidays
    ) is None
    sections = list(Section.objects.filter(school=school, academic_year=year).select_related("class_teacher"))
    marked_today = {s.pk for s in sections if rng.random() < 0.6} if today_is_open else set()

    records = []
    enrolments = Enrollment.objects.filter(school=school, academic_year=year).select_related("section__class_teacher").order_by()
    for e in enrolments:
        teacher = e.section.class_teacher
        marker_id = teacher.user_id if teacher else None
        # Most children miss a few days; a few miss many, so the screens have something to show.
        profile = rng.random()
        p_absent = 0.22 if profile < 0.05 else 0.08 if profile < 0.15 else 0.03
        days = past_days + ([now] if e.section_id in marked_today else [])
        for d in days:
            roll = rng.random()
            if roll < p_absent:
                status = Status.LEAVE if rng.random() < 0.25 else Status.ABSENT
            elif roll < p_absent + 0.03:
                status = Status.LATE
            elif roll < p_absent + 0.045:
                status = Status.HALF_DAY
            else:
                status = Status.PRESENT
            records.append(
                AttendanceRecord(
                    school=school, student_id=e.student_id, section_id=e.section_id, date=d,
                    status=status.value, marked_by_id=marker_id,
                )
            )
    AttendanceRecord.objects.bulk_create(records, batch_size=1000)
    return len(records)
