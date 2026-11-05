"""The access-control engine: RBAC (what you may do) plus scope (to whose records).

    has_perm(user, school, "fees.collect")        -> may this user do this at all?
    visible_students(user, school, "students.view") -> exactly the students they may see

Every view and admin screen goes through these two functions, so scoping rules live in one place.
"""
from django.db.models import Q

from .models import Membership
from .permissions import SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_CHILDREN, SCOPE_SELF


def memberships_for(user, school):
    """The user's active memberships in `school`, with roles loaded.

    Looked up once per user object (i.e. once per request) and reused by the permission checks,
    the navigation and the scoping code, which would otherwise each hit the database.
    """
    if school is None or not getattr(user, "is_authenticated", False):
        return []
    cache = getattr(user, "_membership_cache", None)
    if cache is None:
        cache = user._membership_cache = {}
    if school.pk not in cache:
        cache[school.pk] = list(
            Membership.objects.filter(user=user, school=school, is_active=True).select_related("role")
        )
    return cache[school.pk]


def permissions_for(user, school, all_scope=False):
    perms = set()
    for membership in memberships_for(user, school):
        if all_scope and membership.role.scope != SCOPE_ALL:
            continue
        perms.update(membership.role.permissions)
    return perms


def has_perm(user, school, perm, all_scope=False):
    """`all_scope=True` additionally requires the permission to apply school-wide (used by the admin site)."""
    return perm in permissions_for(user, school, all_scope=all_scope)


def scopes_for(user, school, perm=None):
    return {
        m.role.scope
        for m in memberships_for(user, school)
        if perm is None or perm in m.role.permissions
    }


def _scope_q(user, school, scope):
    """A Q filter on Student for one scope. None means 'nothing'."""
    from academics.models import Section
    from people.models import Staff

    if scope == SCOPE_SELF:
        return Q(user=user)
    if scope == SCOPE_CHILDREN:
        return Q(guardian_links__guardian__user=user)
    if scope == SCOPE_ASSIGNED:
        staff = Staff.objects.filter(school=school, user=user, is_active=True).values("pk")
        sections = (
            Section.objects.filter(school=school, academic_year__is_current=True)
            .filter(Q(class_teacher__in=staff) | Q(assignments__teacher__in=staff))
            .values("pk")
        )
        return Q(enrollments__section__in=sections, enrollments__academic_year__is_current=True)
    return None


def students_in_scope(user, school, scope):
    """Students reachable through one specific scope (used to build role-specific dashboard panels)."""
    from people.models import Student

    base = Student.objects.filter(school=school)
    if scope == SCOPE_ALL:
        return base
    q = _scope_q(user, school, scope)
    return base.none() if q is None else base.filter(q).distinct()


def visible_students(user, school, perm="students.view"):
    """Union of everything the user's roles *that grant `perm`* allow them to see."""
    from people.models import Student

    base = Student.objects.filter(school=school)
    combined = Q()
    found = False
    for membership in memberships_for(user, school):
        role = membership.role
        if perm not in role.permissions:
            continue
        if role.scope == SCOPE_ALL:
            return base
        q = _scope_q(user, school, role.scope)
        if q is not None:
            combined |= q
            found = True
    return base.filter(combined).distinct() if found else base.none()


def visible_sections(user, school, perm):
    """Class sections (current year) whose class-level data the user may see: school-wide roles see all,
    teachers see the sections they teach. Parents and students get none: class registers are not theirs."""
    from academics.models import Section
    from people.models import Staff

    base = Section.objects.filter(school=school, academic_year__is_current=True)
    combined = Q()
    found = False
    for membership in memberships_for(user, school):
        role = membership.role
        if perm not in role.permissions:
            continue
        if role.scope == SCOPE_ALL:
            return base
        if role.scope == SCOPE_ASSIGNED:
            staff = Staff.objects.filter(school=school, user=user, is_active=True).values("pk")
            combined |= Q(class_teacher__in=staff) | Q(assignments__teacher__in=staff)
            found = True
    return base.filter(combined).distinct() if found else base.none()


def can_view_student(user, school, student, perm="students.view"):
    return visible_students(user, school, perm).filter(pk=student.pk).exists()
