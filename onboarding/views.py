from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from academics.models import Grade
from academics.queries import with_current_class
from accounts.access import has_perm
from accounts.decorators import require_any_perm
from accounts.models import Membership, Role, User
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED
from people.models import Guardian, Staff, Student

from . import importing, provisioning
from .models import AccountEvent, ImportBatch
from .provisioning import ProvisionError

# URL word -> (kind used by the services, heading, permission needed)
KINDS = {
    "guardians": ("guardian", "Parents and guardians", "students.manage"),
    "students": ("student", "Students", "students.manage"),
    "staff": ("staff", "Staff", "staff.manage"),
}
IMPORT_PERMISSION = {"students": "students.manage", "staff": "staff.manage"}
PER_PAGE = 40


def _authorize(request, perm):
    if not has_perm(request.user, request.school, perm, all_scope=True):
        raise PermissionDenied


def _kind(request, word):
    if word not in KINDS:
        raise Http404
    kind, heading, perm = KINDS[word]
    _authorize(request, perm)
    return kind, heading


def _safe_next(request, default):
    target = request.POST.get("next") or ""
    return target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else default


def _login_id(kind, user):
    return (user.phone or user.username) if kind == "guardian" else user.username


def _slip(request, kind, person, user, password, role_name, *, linked=False, extra=""):
    return {
        "name": person.full_name if person else (user.get_full_name() or user.username), "role": role_name,
        "login_id": _login_id(kind, user), "password": password, "linked": linked, "extra": extra,
    }


def _credentials_page(request, slips, *, heading, skipped=(), summary=None):
    """Temporary passwords are shown on THIS response only (never stored readably, never cached)."""
    response = render(
        request, "onboarding/credentials.html",
        {"slips": slips, "heading": heading, "skipped": skipped, "summary": summary, "login_url": request.build_absolute_uri("/")},
    )
    response["Cache-Control"] = "no-store"
    return response


def _extra_for(kind, person):
    if kind == "guardian":
        names = [link.student.full_name for link in person.student_links.select_related("student")]
        return "Parent of " + ", ".join(names) if names else ""
    if kind == "student":
        enrolment = person.enrollments.filter(academic_year__is_current=True).select_related("section__grade").first()
        return str(enrolment.section) if enrolment else ""
    return person.designation


# -- the hub ---------------------------------------------------------------------------------------------

@require_any_perm("students.manage", "staff.manage", all_scope=True)
def hub(request):
    school, user = request.school, request.user
    return render(
        request, "onboarding/hub.html",
        {
            "counts": provisioning.hub_counts(school),
            "events": AccountEvent.objects.filter(school=school).select_related("actor")[:12],
            "batches": ImportBatch.objects.filter(school=school)[:5],
            "can_students": has_perm(user, school, "students.manage", all_scope=True),
            "can_staff": has_perm(user, school, "staff.manage", all_scope=True),
        },
    )


# -- people and their logins ------------------------------------------------------------------------------------

def _people_queryset(school, kind):
    if kind == "guardian":
        return Guardian.objects.filter(school=school).select_related("user").prefetch_related("student_links__student")
    if kind == "staff":
        return Staff.objects.filter(school=school, is_active=True).select_related("user")
    return with_current_class(Student.objects.filter(school=school, status=Student.Status.ACTIVE)).select_related("user")


@require_any_perm("students.manage", "staff.manage", all_scope=True)
def people(request, word):
    school = request.school
    kind, heading = _kind(request, word)
    qs = _people_queryset(school, kind)
    q = request.GET.get("q", "").strip()
    if q:
        extra = Q(phone__icontains=q) if kind in ("guardian", "staff") else Q(admission_no__icontains=q)
        qs = qs.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | extra)
    status = request.GET.get("status", "")
    filters = {
        "none": Q(user__isnull=True),
        "inactive": Q(user__is_active=False),
        "must_change": Q(user__is_active=True, user__must_change_password=True),
        "active": Q(user__is_active=True, user__must_change_password=False),
    }
    if status in filters:
        qs = qs.filter(filters[status])
    page = Paginator(qs.order_by("first_name", "last_name", "pk"), PER_PAGE).get_page(request.GET.get("page"))

    user_ids = [p.user_id for p in page.object_list if p.user_id]
    roles = {}
    for m in Membership.objects.filter(school=school, user_id__in=user_ids).select_related("role"):
        roles.setdefault(m.user_id, []).append(m.role.name)
    rows = []
    for person in page.object_list:
        code, label = provisioning.login_status(person)
        row = {"person": person, "status": code, "label": label, "roles": roles.get(person.user_id, [])}
        if kind == "guardian":
            row["children"] = ", ".join(link.student.full_name for link in person.student_links.all())
        if person.user_id:
            ok, _ = provisioning.can_manage_account(request.user, school, person.user)
            row["can_manage"] = ok
        rows.append(row)
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request, "onboarding/people.html",
        {
            "word": word, "kind": kind, "heading": heading, "rows": rows, "page": page, "q": q, "status": status,
            "query_string": params.urlencode(),
            "staff_roles": Role.objects.filter(school=school, scope__in=[SCOPE_ALL, SCOPE_ASSIGNED]).order_by("name") if kind == "staff" else [],
        },
    )


@require_any_perm("students.manage", "staff.manage", all_scope=True)
@require_POST
def create_person_login(request, word, pk):
    school = request.school
    kind, _heading = _kind(request, word)
    person = get_object_or_404(provisioning.PERSON_MODELS[kind].objects.filter(school=school), pk=pk)
    role_id = request.POST.get("role") if kind == "staff" else None
    try:
        result = provisioning.create_login(school, kind, person, request.user, role_id=int(role_id) if role_id and role_id.isdigit() else None)
    except ProvisionError as error:
        messages.error(request, str(error))
        return redirect(_safe_next(request, reverse("setup_people", args=[word])))
    slip = _slip(request, kind, person, result.user, result.password, result.role.name, linked=result.linked, extra=_extra_for(kind, person))
    return _credentials_page(request, [slip], heading=f"Login for {person.full_name}")


@require_any_perm("students.manage", "staff.manage", all_scope=True)
@require_POST
def account_action(request, user_pk, action):
    school = request.school
    target = get_object_or_404(User, pk=user_pk)
    if not Membership.objects.filter(user=target, school=school).exists():
        raise Http404  # someone else's account: it simply doesn't exist for you
    back = _safe_next(request, reverse("setup_home"))
    try:
        if action == "reset":
            password = provisioning.reset_password(school, target, request.user)
            roles = ", ".join(Membership.objects.filter(user=target, school=school).values_list("role__name", flat=True))
            kind = "guardian" if target.guardian_profiles.filter(school=school).exists() else "student"
            return _credentials_page(
                request, [_slip(request, kind, None, target, password, roles)], heading=f"New password for {target.get_full_name() or target.username}"
            )
        if action == "deactivate":
            provisioning.set_active(school, target, request.user, False)
            messages.success(request, f"{target.get_full_name() or target.username} can no longer sign in.")
        elif action == "activate":
            provisioning.set_active(school, target, request.user, True)
            messages.success(request, f"{target.get_full_name() or target.username} can sign in again.")
        else:
            raise Http404
    except ProvisionError as error:
        messages.error(request, str(error))
    return redirect(back)


@require_any_perm("students.manage", "staff.manage", all_scope=True)
def bulk(request, word):
    school = request.school
    kind, heading = _kind(request, word)
    grades = Grade.objects.filter(school=school).order_by("order") if kind == "student" else []
    staff_roles = Role.objects.filter(school=school, scope__in=[SCOPE_ALL, SCOPE_ASSIGNED]).order_by("name") if kind == "staff" else []

    if kind == "guardian":
        people_qs = Guardian.objects.filter(school=school, user__isnull=True, student_links__student__status=Student.Status.ACTIVE).distinct()
    elif kind == "staff":
        people_qs = Staff.objects.filter(school=school, user__isnull=True, is_active=True)
    else:
        people_qs = Student.objects.filter(school=school, user__isnull=True, status=Student.Status.ACTIVE)
        grade_id = (request.POST.get("grade") or request.GET.get("grade") or "")
        if grade_id.isdigit():
            people_qs = people_qs.filter(enrollments__academic_year__is_current=True, enrollments__section__grade_id=int(grade_id))
    people_list = list(people_qs.order_by("first_name", "last_name", "pk")[: provisioning.BULK_LIMIT + 1])
    over_limit = len(people_list) > provisioning.BULK_LIMIT
    people_list = people_list[: provisioning.BULK_LIMIT]
    no_phone = sum(1 for p in people_list if kind == "guardian" and len("".join(c for c in p.phone if c.isdigit())) < 10)

    if request.method == "POST":
        role_id = request.POST.get("role", "")
        try:
            results, skipped = provisioning.bulk_create_logins(
                school, kind, request.user, people_list, role_id=int(role_id) if role_id.isdigit() else None
            )
        except ProvisionError as error:  # e.g. no permission, no such role
            messages.error(request, str(error))
            return redirect("setup_bulk", word=word)
        if not results and skipped:
            messages.error(request, skipped[0][1])
            return redirect("setup_bulk", word=word)
        slips = [_slip(request, kind, r.person, r.user, r.password, r.role.name, linked=r.linked, extra=_extra_for(kind, r.person)) for r in results]
        return _credentials_page(request, slips, heading=f"{heading}: new logins", skipped=[(p.full_name, why) for p, why in skipped])
    return render(
        request, "onboarding/bulk.html",
        {"word": word, "kind": kind, "heading": heading, "count": len(people_list), "no_phone": no_phone, "over_limit": over_limit,
         "grades": grades, "grade_id": request.GET.get("grade", ""), "staff_roles": staff_roles},
    )


# -- importing ------------------------------------------------------------------------------------------------------

@require_any_perm("students.manage", "staff.manage", all_scope=True)
def import_start(request):
    school, user = request.school, request.user
    can = {word: has_perm(user, school, perm, all_scope=True) for word, perm in IMPORT_PERMISSION.items()}
    if request.method == "POST":
        word = request.POST.get("kind", "")
        if word not in IMPORT_PERMISSION:
            raise Http404
        _authorize(request, IMPORT_PERMISSION[word])
        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, "Choose a CSV file to upload.")
            return redirect("setup_import")
        raw = upload.read(importing.MAX_BYTES + 1)
        try:
            stage = importing.stage_students if word == "students" else importing.stage_staff
            batch = stage(school, user, upload.name, raw)
        except importing.ImportFileError as error:
            messages.error(request, str(error))
            return redirect("setup_import")
        return redirect("setup_import_preview", pk=batch.pk)
    return render(request, "onboarding/import_start.html", {"can": can, "recent": ImportBatch.objects.filter(school=school)[:5]})


def _batch(request, pk):
    batch = get_object_or_404(ImportBatch.objects.filter(school=request.school), pk=pk)
    _authorize(request, IMPORT_PERMISSION[batch.kind])
    return batch


@require_any_perm("students.manage", "staff.manage", all_scope=True)
def import_preview(request, pk):
    batch = _batch(request, pk)
    school = request.school
    if request.method == "POST":
        try:
            if batch.kind == ImportBatch.Kind.STUDENTS:
                result = importing.apply_students(batch)
            else:
                result = importing.apply_staff(batch)
        except importing.ImportFileError as error:
            messages.error(request, str(error))
            return redirect("setup_import_preview", pk=batch.pk)
        if batch.kind == ImportBatch.Kind.STUDENTS and request.POST.get("create_logins") == "on":
            guardians = Guardian.objects.filter(
                school=school, user__isnull=True, student_links__student_id__in=result["student_ids"]
            ).distinct().order_by("first_name", "last_name", "pk")
            results, skipped = provisioning.bulk_create_logins(school, "guardian", request.user, guardians)
            slips = [_slip(request, "guardian", r.person, r.user, r.password, r.role.name, linked=r.linked, extra=_extra_for("guardian", r.person)) for r in results]
            return _credentials_page(
                request, slips, heading="Imported: parent logins",
                skipped=[(p.full_name, why) for p, why in skipped],
                summary=f"{result['students']} students were imported and {len(slips)} parent logins created.",
            )
        messages.success(request, f"Imported {result.get('students', result.get('staff'))} {'students' if batch.kind == 'students' else 'staff'}.")
        return redirect("setup_import_preview", pk=batch.pk)
    return render(
        request, "onboarding/import_preview.html",
        {"batch": batch, "problems": batch.problems[:50], "sample": batch.rows[:15],
         "warned": [r for r in batch.rows if r["warnings"]][:30], "can_provision": has_perm(request.user, school, "students.manage", all_scope=True)},
    )


@require_any_perm("students.manage", "staff.manage", all_scope=True)
def import_problems(request, pk):
    batch = _batch(request, pk)
    response = HttpResponse(importing.problems_csv(batch), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="rows-to-fix-{batch.pk}.csv"'
    return response


@require_any_perm("students.manage", "staff.manage", all_scope=True)
def import_template(request, word):
    if word not in IMPORT_PERMISSION:
        raise Http404
    _authorize(request, IMPORT_PERMISSION[word])
    response = HttpResponse(importing.template_csv(word), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{word}-template.csv"'
    return response
