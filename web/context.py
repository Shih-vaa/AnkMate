from django.conf import settings

from accounts.access import memberships_for, permissions_for, scopes_for
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_CHILDREN, SCOPE_SELF

_ACTIVE_NAV = {
    "dashboard": "dashboard",
    "student_list": "students",
    "student_detail": "students",
    "fee_structure": "fees",
}


def students_label(scopes):
    """Same screen, wording that fits the person looking at it."""
    if SCOPE_ALL in scopes:
        return "Students"
    if SCOPE_ASSIGNED in scopes:
        return "My students"
    if SCOPE_CHILDREN in scopes:
        return "My children"
    if SCOPE_SELF in scopes:
        return "My profile"
    return "Students"


def app_context(request):
    ctx = {"PRODUCT_NAME": settings.PRODUCT_NAME, "school": None, "nav": [], "role_names": []}
    school = getattr(request, "school", None)
    user = getattr(request, "user", None)
    if not (school and user and user.is_authenticated):
        return ctx

    perms = permissions_for(user, school)
    nav = [{"key": "dashboard", "label": "Home", "url_name": "dashboard"}]
    if "students.view" in perms:
        label = students_label(scopes_for(user, school, "students.view"))
        nav.append({"key": "students", "label": label, "url_name": "student_list"})
    if "fees.view" in perms:
        nav.append({"key": "fees", "label": "Fees", "url_name": "fee_structure"})

    match = getattr(request, "resolver_match", None)
    ctx.update(
        school=school,
        nav=nav,
        active_nav=_ACTIVE_NAV.get(match.url_name if match else None),
        role_names=[m.role.name for m in memberships_for(user, school)],
        show_admin_link=user.is_staff,
    )
    return ctx
