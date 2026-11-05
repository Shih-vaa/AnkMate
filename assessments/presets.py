"""Load the exam structure and grade scale from a preset (called by core.presets.loader.apply_preset)."""
from datetime import date
from decimal import Decimal

from . import services
from .models import Exam, GradeBand


def apply_assessment_preset(school, year, data):
    """Create the preset's grade scale, exams, and an (empty) paper for every subject of every class. Idempotent."""
    for label, minimum in data.get("grade_bands", []):
        GradeBand.objects.get_or_create(school=school, label=label, defaults={"min_percent": Decimal(minimum)})
    for spec in data.get("exams", []):
        month, day = spec["starts"]
        start_year = year.start_date.year if month >= year.start_date.month else year.start_date.year + 1
        exam, _ = Exam.objects.get_or_create(
            school=school, academic_year=year, name=spec["name"],
            defaults={"order": spec["order"], "weightage": Decimal(spec["weightage"]), "starts_on": date(start_year, month, day)},
        )
        services.create_papers(exam, Decimal(spec["max_marks"]))
