"""Starting roles for a new school. The school can edit these afterwards (they are rows)."""
from .models import Role
from .permissions import PERMISSIONS, SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_CHILDREN, SCOPE_SELF

DEFAULT_ROLES = [
    {
        "code": "admin",
        "name": "Principal / Admin",
        "scope": SCOPE_ALL,
        "permissions": sorted(PERMISSIONS),
    },
    {
        "code": "office",
        "name": "Office staff",
        "scope": SCOPE_ALL,
        "permissions": [
            "students.view", "students.manage", "staff.view", "attendance.view",
            "fees.view", "fees.collect", "notices.view", "notices.post",
        ],
    },
    {
        "code": "accountant",
        "name": "Accountant",
        "scope": SCOPE_ALL,
        "permissions": ["students.view", "fees.view", "fees.collect", "fees.manage", "notices.view"],
    },
    {
        "code": "teacher",
        "name": "Teacher",
        "scope": SCOPE_ASSIGNED,
        "permissions": [
            "students.view", "attendance.view", "attendance.mark",
            "marks.view", "marks.enter", "notices.view", "notices.post",
        ],
    },
    {
        "code": "parent",
        "name": "Parent / Guardian",
        "scope": SCOPE_CHILDREN,
        "permissions": ["students.view", "attendance.view", "marks.view", "fees.view", "notices.view"],
    },
    {
        "code": "student",
        "name": "Student",
        "scope": SCOPE_SELF,
        "permissions": ["students.view", "attendance.view", "marks.view", "notices.view"],
    },
]


def ensure_default_roles(school):
    roles = {}
    for spec in DEFAULT_ROLES:
        role, _ = Role.objects.get_or_create(
            school=school,
            code=spec["code"],
            defaults={"name": spec["name"], "scope": spec["scope"], "permissions": spec["permissions"]},
        )
        roles[spec["code"]] = role
    return roles
