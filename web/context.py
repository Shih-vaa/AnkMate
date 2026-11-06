from django.conf import settings

from accounts.access import memberships_for, permissions_for, scopes_for
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_CHILDREN, SCOPE_SELF

_ACTIVE_NAV = {
    "dashboard": "dashboard",
    "student_list": "students",
    "student_detail": "students",
    "attendance_home": "attendance",
    "attendance_mark": "attendance",
    "attendance_register": "attendance",
    "attendance_student": "attendance",
    "marks_home": "marks",
    "marks_exams": "marks",
    "marks_entry_index": "marks",
    "marks_enter": "marks",
    "marks_sheet_index": "marks",
    "marks_sheet": "marks",
    "marks_student": "marks",
    "marks_report": "marks",
    "setup_home": "setup",
    "setup_people": "setup",
    "setup_bulk": "setup",
    "setup_import": "setup",
    "setup_import_preview": "setup",
    "fee_structure": "fees",
    "fee_home": "fees",
    "fee_student": "fees",
    "fee_invoice": "fees",
    "fee_receipt": "fees",
    "fee_collect": "fees",
    "fee_collect_student": "fees",
    "fee_generate": "fees",
    "fee_dues": "fees",
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
    if "attendance.view" in perms or "attendance.mark" in perms:
        nav.append({"key": "attendance", "label": "Attendance", "url_name": "attendance_home"})
    if "marks.view" in perms or "marks.enter" in perms:
        nav.append({"key": "marks", "label": "Marks", "url_name": "marks_home"})
    if "fees.view" in perms:
        nav.append({"key": "fees", "label": "Fees", "url_name": "fee_home"})

    if permissions_for(user, school, all_scope=True) & {"students.manage", "staff.manage"}:
        nav.append({"key": "setup", "label": "People & logins", "url_name": "setup_home"})

    match = getattr(request, "resolver_match", None)
    ctx.update(
        school=school,
        nav=nav,
        active_nav=_ACTIVE_NAV.get(match.url_name if match else None),
        role_names=[m.role.name for m in memberships_for(user, school)],
        show_admin_link=user.is_staff,
    )
    return ctx
