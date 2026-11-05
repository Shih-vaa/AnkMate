"""Load a preset (academic structure + fee structure + default roles) into a school.

    apply_preset(school, "icse")

The code here knows nothing about ICSE; all board-specific facts live in the preset module.
"""
import importlib
from datetime import date, timedelta

from django.db import transaction

PRESETS = {"icse": "core.presets.icse"}


def current_session_start_year(today=None):
    """The calendar year in which the running April-March session started."""
    today = today or date.today()
    return today.year if today.month >= 4 else today.year - 1


@transaction.atomic
def apply_preset(school, key, session_start_year=None):
    from academics.models import AcademicYear, Grade, GradeSubject, Section, Subject
    from accounts.defaults import ensure_default_roles
    from fees.models import FeeHead, FeeStructure

    if key not in PRESETS:
        raise ValueError(f"Unknown preset '{key}'. Available: {', '.join(sorted(PRESETS))}")
    data = importlib.import_module(PRESETS[key]).PRESET

    ensure_default_roles(school)

    start_year = session_start_year or current_session_start_year()
    starts = date(start_year, data["year_start_month"], data["year_start_day"])
    next_starts = date(start_year + 1, data["year_start_month"], data["year_start_day"])
    year, _ = AcademicYear.objects.get_or_create(
        school=school,
        name=f"{start_year}-{str(start_year + 1)[-2:]}",
        defaults={"start_date": starts, "end_date": next_starts - timedelta(days=1), "is_current": True},
    )

    grades = {}
    for name, order, stage in data["grades"]:
        grades[name], _ = Grade.objects.get_or_create(school=school, name=name, defaults={"order": order, "stage": stage})

    subjects = {}
    for code, name in data["subjects"]:
        subjects[code], _ = Subject.objects.get_or_create(school=school, code=code, defaults={"name": name})

    for grade in grades.values():
        for code in data["stage_subjects"].get(grade.stage, []):
            GradeSubject.objects.get_or_create(school=school, grade=grade, subject=subjects[code])
        for section_name in data["sections"]:
            Section.objects.get_or_create(school=school, academic_year=year, grade=grade, name=section_name)

    heads = {}
    for code, name in data["fee_heads"]:
        heads[code], _ = FeeHead.objects.get_or_create(school=school, code=code, defaults={"name": name})

    for grade in grades.values():
        for head_code, amount, frequency in data["fees_by_stage"].get(grade.stage, []):
            FeeStructure.objects.get_or_create(
                school=school, academic_year=year, grade=grade, head=heads[head_code],
                defaults={"amount": amount, "frequency": frequency, "notes": "Dummy demo value"},
            )

    from assessments.presets import apply_assessment_preset

    apply_assessment_preset(school, year, data)
    return year
