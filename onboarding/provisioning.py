"""Creating and managing logins for the school's people.

RULES
  * Office staff (students.manage) create parent and student logins; the principal (staff.manage) creates staff logins.
  * A new login gets a random temporary password, shown ONCE to whoever creates it (for a printed slip). The person
    must replace it at first sign-in. Passwords are never stored in readable form.
  * A phone number that already has a login is never given a second one: the new role is added to the existing login
    (a teacher who is also a parent signs in once) and their password is left alone.
  * You cannot manage an account more powerful than you may manage: office staff can only reset or deactivate logins
    that hold nothing but parent/student roles; only the principal can touch staff logins. Nobody can act on their own
    account, on a platform administrator, or on a login that is also used at another school.
  * Every action is written to the audit log.
"""
import re
import secrets
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.access import has_perm
from accounts.models import Membership, Role, User, normalize_phone
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_CHILDREN, SCOPE_SELF
from people.models import Guardian, Staff, Student

from .models import AccountEvent

# The characters a temporary code may use: no 0/O, 1/I/L, since it is read off a slip and typed on a phone.
TEMP_CODE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

PERSON_MODELS = {"guardian": Guardian, "staff": Staff, "student": Student}
PERMISSION_NEEDED = {"guardian": "students.manage", "student": "students.manage", "staff": "staff.manage"}
FIXED_ROLE_CODE = {"guardian": "parent", "student": "student"}
LOW_SCOPES = {SCOPE_CHILDREN, SCOPE_SELF}
BULK_LIMIT = 1000


class ProvisionError(ValueError):
    """A rule was broken; the message is written to be shown to the person who tried."""


@dataclass
class LoginResult:
    person: object
    kind: str
    user: User
    password: str | None  # None when an existing login was reused: their own password still works
    linked: bool
    role: Role


def generate_password():
    chars = "".join(secrets.choice(TEMP_CODE_CHARS) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _label(user):
    return user.get_full_name() or user.username


def _log(school, actor, user, action, detail=""):
    AccountEvent.objects.create(
        school=school, actor=actor, target=user, target_label=_label(user), action=action, detail=detail[:200]
    )


# -- who may do what ------------------------------------------------------------------------------

def can_provision(actor, school, kind):
    return has_perm(actor, school, PERMISSION_NEEDED[kind], all_scope=True)


def can_manage_account(actor, school, target):
    """(allowed, reason) for resetting, deactivating or reactivating `target`'s login."""
    if actor.pk == target.pk:
        return False, "Use Change password for your own account."
    if target.is_superuser:
        return False, "That is a platform administrator's account."
    if Membership.objects.filter(user=target).exclude(school=school).exists():
        return False, "This login is also used at another school, so only the platform owner can change it."
    memberships = list(Membership.objects.filter(user=target, school=school).select_related("role"))
    if not memberships:
        return False, "That person has no role at this school."
    if has_perm(actor, school, "school.manage", all_scope=True):
        return True, ""
    if has_perm(actor, school, "students.manage", all_scope=True) and all(m.role.scope in LOW_SCOPES for m in memberships):
        return True, ""
    return False, "Only the principal can manage staff logins."


def role_for(school, kind, role_id=None):
    if kind in FIXED_ROLE_CODE:
        role = Role.objects.filter(school=school, code=FIXED_ROLE_CODE[kind]).first()
        if role is None:
            raise ProvisionError(f"This school has no '{FIXED_ROLE_CODE[kind]}' role yet. Add it in Manage data first.")
        return role
    role = Role.objects.filter(school=school, pk=role_id).first() if role_id else None
    if role is None:
        raise ProvisionError("Choose a role for this staff member.")
    if role.scope not in (SCOPE_ALL, SCOPE_ASSIGNED):
        raise ProvisionError("Staff logins need a staff role (for example Teacher or Office staff).")
    return role


# -- creating logins ----------------------------------------------------------------------------------

def _unique_username(base):
    base = re.sub(r"[^a-z0-9._-]+", "", (base or "").lower()) or "user"
    candidate, n = base, 1
    while User.objects.filter(username__iexact=candidate).exists():
        n += 1
        candidate = f"{base}{n}"
    return candidate


def _username_hint(kind, person):
    if kind == "student":
        return person.admission_no
    if kind == "staff":
        return person.employee_code
    return normalize_phone(person.phone)


@transaction.atomic
def create_login(school, kind, person, actor, *, role_id=None):
    """Give one person a login (or link them to the login their phone number already has)."""
    if not can_provision(actor, school, kind):
        raise ProvisionError("You don't have permission to create these logins.")
    if person.school_id != school.id:
        raise ProvisionError("That person doesn't belong to this school.")
    if person.user_id:
        raise ProvisionError(f"{person.full_name} already has a login. Use Reset password if they can't sign in.")
    role = role_for(school, kind, role_id)

    phone = normalize_phone(getattr(person, "phone", "")) if kind != "student" else ""
    if kind == "guardian" and len(phone) != 10:
        raise ProvisionError(f"{person.full_name} has no valid 10-digit phone number. Parents sign in with their phone number, so add it first.")
    if len(phone) != 10:
        phone = ""  # staff and students may have a login without a phone number: they use their username

    existing = User.objects.filter(phone=phone).first() if phone else None
    if existing:
        if existing.is_superuser:
            raise ProvisionError(f"The phone number {phone} belongs to a platform administrator's account.")
        if not existing.is_active:
            raise ProvisionError(f"A deactivated login already uses {phone}. Reactivate it instead of creating a new one.")
        clash = PERSON_MODELS[kind].objects.filter(school=school, user=existing).first()
        if clash:
            raise ProvisionError(f"The login for {phone} is already linked to {clash.full_name}. Two records share this phone number.")
        user, password, linked = existing, None, True
    else:
        user = User(
            username=_unique_username(_username_hint(kind, person)), first_name=person.first_name[:150], last_name=person.last_name[:150],
            phone=phone or None, email=getattr(person, "email", "") or "", is_active=True, must_change_password=True,
        )
        password = generate_password()
        user.set_password(password)
        user.save()
        linked = False

    membership, created = Membership.objects.get_or_create(user=user, role=role, defaults={"school": school})
    if not created and not membership.is_active:
        membership.is_active = True
        membership.save()
    person.user = user
    person.save(update_fields=["user"])
    _log(school, actor, user, AccountEvent.Action.LINKED if linked else AccountEvent.Action.CREATED,
         f"{person.full_name} as {role.name}")
    return LoginResult(person, kind, user, password, linked, role)


def bulk_create_logins(school, kind, actor, persons, *, role_id=None):
    """Create logins for many people. Returns (results, skipped) where skipped is [(person, reason)].
    One person's problem never stops the rest."""
    results, skipped = [], []
    for person in list(persons)[:BULK_LIMIT]:
        try:
            results.append(create_login(school, kind, person, actor, role_id=role_id))
        except ProvisionError as error:
            skipped.append((person, str(error)))
    return results, skipped


# -- managing existing logins -----------------------------------------------------------------------------

@transaction.atomic
def reset_password(school, target, actor):
    """Replace a forgotten password with a new temporary one. Returns the new password (shown once)."""
    ok, why = can_manage_account(actor, school, target)
    if not ok:
        raise ProvisionError(why)
    if not target.is_active:
        raise ProvisionError("This login is deactivated. Reactivate it first.")
    password = generate_password()
    target.set_password(password)
    target.must_change_password = True
    target.save(update_fields=["password", "must_change_password"])
    _log(school, actor, target, AccountEvent.Action.RESET)
    return password


@transaction.atomic
def set_active(school, target, actor, active):
    ok, why = can_manage_account(actor, school, target)
    if not ok:
        raise ProvisionError(why)
    if target.is_active == active:
        return target
    target.is_active = active
    target.save(update_fields=["is_active"])
    _log(school, actor, target, AccountEvent.Action.ACTIVATED if active else AccountEvent.Action.DEACTIVATED)
    return target


# -- what the screens need -----------------------------------------------------------------------------

def login_status(person):
    """('none' | 'active' | 'must_change' | 'inactive', label)"""
    user = person.user
    if user is None:
        return "none", "No login"
    if not user.is_active:
        return "inactive", "Deactivated"
    if user.must_change_password:
        return "must_change", "Temporary password"
    return "active", "Active"


def hub_counts(school):
    active_students = Student.objects.filter(school=school, status=Student.Status.ACTIVE)
    users_here = User.objects.filter(memberships__school=school).distinct()
    return {
        "guardians_without_login": Guardian.objects.filter(
            school=school, user__isnull=True, student_links__student__status=Student.Status.ACTIVE
        ).distinct().count(),
        "staff_without_login": Staff.objects.filter(school=school, user__isnull=True, is_active=True).count(),
        "students_without_login": active_students.filter(user__isnull=True).count(),
        "temporary_passwords": users_here.filter(must_change_password=True, is_active=True).count(),
        "deactivated": users_here.filter(is_active=False).count(),
    }
