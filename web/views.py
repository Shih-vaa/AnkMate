from collections import OrderedDict
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string

from academics.models import AcademicYear, Enrollment, Grade, Section, TeachingAssignment
from accounts.access import scopes_for, students_in_scope, visible_students
from accounts.decorators import require_perm
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_CHILDREN, SCOPE_SELF
from fees.models import FeeStructure
from people.models import Guardian, Staff, Student


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


@login_required
def dashboard(request):
    school, user = request.school, request.user
    if school is None:
        return render(request, "web/no_school.html", status=403)

    scopes = scopes_for(user, school)
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    ctx = {"year": year}

    if SCOPE_ALL in scopes:
        strength = (
            Enrollment.objects.filter(school=school, academic_year__is_current=True)
            .values("section__grade__name", "section__grade__order")
            .annotate(n=Count("id"))
            .order_by("section__grade__order")
        )
        ctx["school_stats"] = {
            "students": Student.objects.filter(school=school, status=Student.Status.ACTIVE).count(),
            "staff": Staff.objects.filter(school=school, is_active=True).count(),
            "guardians": Guardian.objects.filter(school=school).count(),
            "sections": Section.objects.filter(school=school, academic_year__is_current=True).count(),
        }
        ctx["strength"] = list(strength)

    if SCOPE_ASSIGNED in scopes:
        my_staff = list(Staff.objects.filter(school=school, user=user, is_active=True).values_list("pk", flat=True))
        sections = (
            Section.objects.filter(school=school, academic_year__is_current=True)
            .filter(Q(class_teacher__in=my_staff) | Q(assignments__teacher__in=my_staff))
            .select_related("grade")
            .annotate(n_students=Count("enrollments", distinct=True))
            .prefetch_related(
                Prefetch(
                    "assignments",
                    queryset=TeachingAssignment.objects.filter(teacher__in=my_staff).select_related("subject"),
                    to_attr="my_assignments",
                )
            )
            .distinct()
        )
        ctx["my_sections"] = [
            {"section": s, "is_class_teacher": s.class_teacher_id in my_staff, "subjects": [a.subject.name for a in s.my_assignments]}
            for s in sections
        ]

    if SCOPE_CHILDREN in scopes:
        children = with_current_class(students_in_scope(user, school, SCOPE_CHILDREN)).order_by("first_name")
        ctx["children"] = list(children)

    if SCOPE_SELF in scopes:
        ctx["me"] = list(with_current_class(students_in_scope(user, school, SCOPE_SELF)))

    return render(request, "web/dashboard.html", ctx)


@require_perm("students.view")
def student_list(request):
    school, user = request.school, request.user
    visible = visible_students(user, school, "students.view")

    sections = (
        Section.objects.filter(school=school, academic_year__is_current=True, enrollments__student__in=visible.values("pk"))
        .select_related("grade")
        .distinct()
        .order_by("grade__order", "name")
    )

    students = with_current_class(visible)
    q = request.GET.get("q", "").strip()
    if q:
        students = students.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(admission_no__icontains=q)
        )

    section_id = request.GET.get("section", "")
    selected_section = None
    if section_id.isdigit():
        selected_section = next((s for s in sections if s.pk == int(section_id)), None)
        if selected_section:
            students = students.filter(
                enrollments__section=selected_section, enrollments__academic_year__is_current=True
            )

    students = students.order_by("cur_grade_order", "cur_section", "cur_roll", "first_name")
    page = Paginator(students, 40).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)

    return render(
        request,
        "web/student_list.html",
        {"page": page, "sections": sections, "q": q, "selected_section": selected_section, "query_string": params.urlencode()},
    )


@require_perm("students.view")
def student_detail(request, pk):
    # Looked up through the scoped queryset: a student outside your scope is simply "not found".
    student = get_object_or_404(with_current_class(visible_students(request.user, request.school, "students.view")), pk=pk)
    return render(
        request,
        "web/student_detail.html",
        {
            "student": student,
            "guardian_links": student.guardian_links.select_related("guardian").order_by("-is_primary", "guardian__first_name"),
            "history": student.enrollments.select_related("section__grade", "academic_year").order_by("-academic_year__start_date"),
        },
    )


@require_perm("fees.view")
def fee_structure(request):
    school, user = request.school, request.user
    year = AcademicYear.objects.filter(school=school, is_current=True).first()

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
            group = groups.setdefault(row.grade_id, {"grade": row.grade, "rows": [], "yearly_total": Decimal("0")})
            group["rows"].append(row)
            group["yearly_total"] += row.yearly_amount

    return render(request, "web/fee_structure.html", {"year": year, "groups": list(groups.values())})


# -- PWA plumbing ------------------------------------------------------------------------------

def service_worker(request):
    """Served from the site root so it can control every page. Caches static files only, never user data."""
    response = HttpResponse(render_to_string("web/sw.js", request=request), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response
