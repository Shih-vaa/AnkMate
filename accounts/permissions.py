"""The permission vocabulary.

Permissions are defined in code (so views can rely on them); *which role gets which
permission* is data (Role.permissions) and can differ per school.
"""

PERMISSIONS = {
    "school.manage": "Manage school settings, roles and users",
    "academics.manage": "Manage years, classes, sections, subjects, teaching assignments",
    "students.view": "View student records",
    "students.manage": "Add and edit students, guardians and enrolments",
    "staff.view": "View staff records",
    "staff.manage": "Add and edit staff records",
    "attendance.view": "View attendance",
    "attendance.mark": "Mark attendance",
    "marks.view": "View marks and report cards",
    "marks.enter": "Enter marks",
    "fees.view": "View fee structure, dues and receipts",
    "fees.collect": "Record fee payments",
    "fees.manage": "Manage fee heads and fee structure",
    "notices.view": "Read notices",
    "notices.post": "Post notices",
}

# Scopes decide *whose* records a permission applies to (see accounts.access).
SCOPE_ALL = "all"
SCOPE_ASSIGNED = "assigned"
SCOPE_CHILDREN = "children"
SCOPE_SELF = "self"
