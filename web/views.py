from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string

from academics.models import AcademicYear, Enrollment, Section, TeachingAssignment
from academics.queries import with_current_class
from accounts.access import has_perm, scopes_for, students_in_scope, visible_students
from accounts.decorators import require_perm
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_CHILDREN, SCOPE_SELF
from assessments import services as marks_services
from attendance import services as attendance_services
from fees import services as fee_services
from notices import reminders as notice_reminders
from notices import services as notice_services
from people.models import Guardian, Staff, Student


@login_required
def dashboard(request):
    school, user = request.school, request.user
    if school is None:
        return render(request, "web/no_school.html", status=403)

    scopes = scopes_for(user, school)
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    now = attendance_services.today()
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
        if has_perm(user, school, "attendance.view"):
            ctx["attendance_today"] = attendance_services.school_day_summary(school, now)
        if has_perm(user, school, "fees.view", all_scope=True):
            ctx["fees_summary"] = fee_services.collection_summary(school, fee_services.today())

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
        if has_perm(user, school, "attendance.mark"):
            markable = set(attendance_services.markable_sections(user, school).values_list("pk", flat=True))
            closed = attendance_services.non_working_reason(school, now)
            taken = attendance_services.day_overview(markable, now)
            for item in ctx["my_sections"]:
                sid = item["section"].pk
                item["can_mark"] = sid in markable
                if sid in markable:
                    item["att_state"] = "closed" if closed else ("marked" if taken[sid]["marked"] else "not_marked")

    if SCOPE_CHILDREN in scopes:
        children = with_current_class(students_in_scope(user, school, SCOPE_CHILDREN)).order_by("first_name")
        ctx["children"] = list(children)
        if has_perm(user, school, "attendance.view") and ctx["children"]:
            first, last = attendance_services.month_range(now.year, now.month)
            by_child = attendance_services.percentages_for([c.pk for c in ctx["children"]], first, last)
            for child in ctx["children"]:
                child.att_month = by_child[child.pk]
        if has_perm(user, school, "marks.view") and ctx["children"] and year:
            latest = marks_services.latest_results_for([c.pk for c in ctx["children"]], year, school)
            for child in ctx["children"]:
                child.latest = latest.get(child.pk)
        if has_perm(user, school, "fees.view") and ctx["children"]:
            owed = fee_services.balances_for([c.pk for c in ctx["children"]], fee_services.today())
            for child in ctx["children"]:
                child.fee = owed[child.pk]

    if SCOPE_SELF in scopes:
        ctx["me"] = list(with_current_class(students_in_scope(user, school, SCOPE_SELF)))

    if notice_services.can_view(user, school):
        ctx["new_reminders"] = list(notice_reminders.unread_reminders(user, school)[:3])
        ctx["latest_notices"] = list(notice_services.visible_notices(user, school)[:3])
        ctx["unread_notices"] = set(notice_services.unread_notices(user, school).values_list("pk", flat=True))
        ctx["more_reminders"] = notice_reminders.unread_reminders(user, school).count() - len(ctx["new_reminders"])

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


# -- PWA plumbing ------------------------------------------------------------------------------

def service_worker(request):
    """Served from the site root so it can control every page. Caches static files only, never user data."""
    response = HttpResponse(render_to_string("web/sw.js", request=request), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response
