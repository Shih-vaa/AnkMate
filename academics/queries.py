from django.db.models import OuterRef, Subquery

from .models import Enrollment


def with_current_class(qs):
    """Annotate students with this year's class/section/roll (one query, no per-row lookups)."""
    current = Enrollment.objects.filter(student=OuterRef("pk"), academic_year__is_current=True)
    return qs.annotate(
        cur_grade=Subquery(current.values("section__grade__name")[:1]),
        cur_grade_order=Subquery(current.values("section__grade__order")[:1]),
        cur_section=Subquery(current.values("section__name")[:1]),
        cur_section_id=Subquery(current.values("section_id")[:1]),
        cur_roll=Subquery(current.values("roll_no")[:1]),
    )
