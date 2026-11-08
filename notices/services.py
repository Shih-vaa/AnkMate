"""Rules for notices: who may post to whom, who may read what, and how far a notice reached.

Views stay thin. Two permissions drive everything (their scope says how far they reach):

    notices.view   read notices addressed to you. School-wide roles (principal, office, accountant) read every notice.
                   Teachers read what is addressed to the school, their role or the classes they teach.
                   Parents and students read what is addressed to the school, their role or their child's / their own class.
    notices.post   write notices. School-wide roles may address anyone; a teacher may address only the sections they teach.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from academics.models import Grade, Section, TeachingAssignment
from accounts.access import memberships_for, sections_in_scope
from accounts.models import Membership, Role
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED
from people.models import Student, StudentGuardian

from .models import Notice, NoticeRead, NoticeTarget

TITLE_MAX = 120
BODY_MAX = 4000
UNREAD_WINDOW_DAYS = 30  # older notices are not counted as "new" in the badge, whether or not they were opened


class NoticeError(ValueError):
    """A rule was broken; the message is written to be shown to the person who tried."""


def today():
    """The school's 'today'. A seam: tests replace this to pin the date."""
    return timezone.localdate()


def display_name(user):
    return user.get_full_name() or user.username


# -- who may read ------------------------------------------------------------------------------

def _viewer_memberships(user, school):
    return [m for m in memberships_for(user, school) if "notices.view" in m.role.permissions]


def can_view(user, school):
    return bool(_viewer_memberships(user, school))


def visible_notices(user, school, *, include_dead=False):
    """Exactly the notices this person may read. `include_dead` adds withdrawn and expired ones (for authors and the office)."""
    memberships = _viewer_memberships(user, school)
    qs = Notice.objects.filter(school=school)
    if not memberships:
        return qs.none()
    if not include_dead:
        qs = qs.filter(withdrawn_at__isnull=True).filter(Q(expires_on__isnull=True) | Q(expires_on__gte=today()))
    scopes = {m.role.scope for m in memberships}
    if SCOPE_ALL in scopes:
        return qs

    section_ids = set()
    for scope in scopes:
        section_ids |= set(sections_in_scope(user, school, scope).values_list("pk", flat=True))
    grade_ids = set(Section.objects.filter(pk__in=section_ids).values_list("grade_id", flat=True))
    role_ids = {m.role_id for m in memberships}
    audience = (
        Q(targets__kind=NoticeTarget.Kind.SCHOOL)
        | Q(targets__kind=NoticeTarget.Kind.ROLE, targets__role__in=role_ids)
        | Q(targets__kind=NoticeTarget.Kind.GRADE, targets__grade__in=grade_ids)
        | Q(targets__kind=NoticeTarget.Kind.SECTION, targets__section__in=section_ids)
    )
    return qs.filter(audience | Q(author=user)).distinct()


def get_visible_notice(user, school, pk):
    """The notice if this person may read it (withdrawn and expired ones only for those who manage it), else None."""
    notice = visible_notices(user, school, include_dead=True).filter(pk=pk).first()
    if notice and not notice.is_live(today()) and not can_manage_notice(user, school, notice):
        return None
    return notice


def with_audience(qs):
    """Load each notice's audience rows in one go, so listing the audience of 20 notices is not 20 queries."""
    return qs.prefetch_related(
        Prefetch("targets", queryset=NoticeTarget.objects.select_related("role", "grade", "section__grade"))
    )


def unread_notices(user, school):
    cutoff = timezone.now() - timedelta(days=UNREAD_WINDOW_DAYS)
    return visible_notices(user, school).filter(posted_at__gte=cutoff).exclude(author=user).exclude(reads__user=user)


def mark_read(notice, user):
    NoticeRead.objects.get_or_create(notice=notice, user=user, defaults={"school_id": notice.school_id})


# -- who may post ------------------------------------------------------------------------------

@dataclass
class PostableAudience:
    """What this person may address a notice to."""

    everyone: bool
    roles: object
    grades: object
    sections: object

    @property
    def any(self):
        return self.everyone or self.roles.exists() or self.grades.exists() or self.sections.exists()


def _poster_memberships(user, school):
    return [m for m in memberships_for(user, school) if "notices.post" in m.role.permissions]


def _section_queryset(qs):
    return qs.select_related("grade").order_by("grade__order", "name")


def postable_audience(user, school):
    memberships = _poster_memberships(user, school)
    scopes = {m.role.scope for m in memberships}
    if SCOPE_ALL in scopes:
        return PostableAudience(
            everyone=True,
            roles=Role.objects.filter(school=school).order_by("name"),
            grades=Grade.objects.filter(school=school).order_by("order"),
            sections=_section_queryset(sections_in_scope(user, school, SCOPE_ALL)),
        )
    if SCOPE_ASSIGNED in scopes:  # teachers: only the classes they teach
        return PostableAudience(
            everyone=False,
            roles=Role.objects.none(),
            grades=Grade.objects.none(),
            sections=_section_queryset(sections_in_scope(user, school, SCOPE_ASSIGNED)),
        )
    return PostableAudience(False, Role.objects.none(), Grade.objects.none(), Section.objects.none())


def can_post(user, school):
    return bool(_poster_memberships(user, school)) and postable_audience(user, school).any


def can_manage_notice(user, school, notice):
    """Edit or withdraw: school-wide posters can for any notice, other posters only for their own."""
    if notice.school_id != school.id:
        return False
    memberships = _poster_memberships(user, school)
    if any(m.role.scope == SCOPE_ALL for m in memberships):
        return True
    return bool(memberships) and notice.author_id == user.id


# -- writing -----------------------------------------------------------------------------------

def _check_text(title, body, category, expires_on, now_date):
    title, body = (title or "").strip(), (body or "").strip()
    if not title:
        raise NoticeError("Please give the notice a title.")
    if len(title) > TITLE_MAX:
        raise NoticeError(f"Keep the title to {TITLE_MAX} characters or fewer.")
    if not body:
        raise NoticeError("Please write the notice itself.")
    if len(body) > BODY_MAX:
        raise NoticeError(f"Keep the notice to {BODY_MAX} characters or fewer.")
    if category not in Notice.Category.values:
        raise NoticeError("Please choose a category from the list.")
    if expires_on is not None and expires_on < now_date:
        raise NoticeError("The last day to show the notice can't be in the past.")
    return title, body


def _ids(values):
    return {int(v) for v in values}


@transaction.atomic
def create_notice(
    school, author, *, title, body, category=Notice.Category.GENERAL, expires_on=None,
    everyone=False, role_ids=(), grade_ids=(), section_ids=(),
):
    if not _poster_memberships(author, school):
        raise NoticeError("You don't have permission to post notices.")
    title, body = _check_text(title, body, category, expires_on, today())
    allowed = postable_audience(author, school)

    if everyone and not allowed.everyone:
        raise NoticeError("You can send notices to the classes you teach only.")
    role_ids, grade_ids, section_ids = _ids(role_ids), _ids(grade_ids), _ids(section_ids)
    for wanted, queryset, what in (
        (role_ids, allowed.roles, "role"), (grade_ids, allowed.grades, "class"), (section_ids, allowed.sections, "section"),
    ):
        if wanted - set(queryset.values_list("pk", flat=True)):
            raise NoticeError(f"You can't send a notice to that {what}.")
    if not (everyone or role_ids or grade_ids or section_ids):
        raise NoticeError("Please choose who the notice is for.")

    notice = Notice(
        school=school, title=title, body=body, category=category, expires_on=expires_on,
        author=author, author_name=display_name(author), posted_at=timezone.now(),
    )
    notice.save()
    kind = NoticeTarget.Kind
    if everyone:  # everyone includes every other choice: store just that
        NoticeTarget(school=school, notice=notice, kind=kind.SCHOOL).save()
    else:
        for pk in sorted(role_ids):
            NoticeTarget(school=school, notice=notice, kind=kind.ROLE, role_id=pk).save()
        for pk in sorted(grade_ids):
            NoticeTarget(school=school, notice=notice, kind=kind.GRADE, grade_id=pk).save()
        for pk in sorted(section_ids):
            NoticeTarget(school=school, notice=notice, kind=kind.SECTION, section_id=pk).save()
    return notice


@transaction.atomic
def update_notice(notice, user, *, title, body, category, expires_on=None):
    """Fix the wording, category or last day. The audience can't change: post a new notice to reach other people."""
    if not can_manage_notice(user, notice.school, notice):
        raise NoticeError("You can't change this notice.")
    if notice.is_withdrawn:
        raise NoticeError("This notice has been withdrawn.")
    title, body = _check_text(title, body, category, expires_on, today())
    notice.title, notice.body, notice.category, notice.expires_on = title, body, category, expires_on
    notice.edited_at = timezone.now()
    notice.save()
    return notice


def withdraw_notice(notice, user):
    if not can_manage_notice(user, notice.school, notice):
        raise NoticeError("You can't withdraw this notice.")
    if not notice.is_withdrawn:
        notice.withdrawn_at = timezone.now()
        notice.save(update_fields=["withdrawn_at"])
    return notice


# -- how far a notice reached ------------------------------------------------------------------

@dataclass
class Reach:
    total: int       # people with a login who are in the audience (not counting the author)
    read: int        # of those, how many have opened it
    unreached: int   # students in the audience nobody can reach yet: no parent or student login


def eligible_user_ids(school):
    """Everyone with an active login that lets them read notices in this school."""
    memberships = Membership.objects.filter(school=school, is_active=True, user__is_active=True).select_related("role")
    return {m.user_id for m in memberships if "notices.view" in m.role.permissions}


def reach(notice):
    school = notice.school
    eligible = eligible_user_ids(school)
    targets = list(notice.targets.all())
    candidates, unreached = set(), 0

    role_ids = {t.role_id for t in targets if t.kind == NoticeTarget.Kind.ROLE}
    if role_ids:
        candidates |= set(
            Membership.objects.filter(school=school, is_active=True, role_id__in=role_ids).values_list("user_id", flat=True)
        )

    whole_school = any(t.kind == NoticeTarget.Kind.SCHOOL for t in targets)
    grade_ids = {t.grade_id for t in targets if t.kind == NoticeTarget.Kind.GRADE}
    section_ids = {t.section_id for t in targets if t.kind == NoticeTarget.Kind.SECTION}
    if whole_school:
        candidates |= eligible

    if whole_school or grade_ids or section_ids:
        sections = Section.objects.filter(school=school, academic_year__is_current=True)
        if not whole_school:
            sections = sections.filter(Q(grade_id__in=grade_ids) | Q(pk__in=section_ids))
        section_pks = sections.values("pk")
        # Teachers of those sections.
        candidates |= {
            uid for uid in sections.filter(class_teacher__is_active=True).values_list("class_teacher__user_id", flat=True) if uid
        }
        candidates |= {
            uid
            for uid in TeachingAssignment.objects.filter(section__in=section_pks, teacher__is_active=True)
            .values_list("teacher__user_id", flat=True)
            if uid
        }
        # Parents and students. A student nobody can reach is counted, so the office knows to create a login.
        students = Student.objects.filter(
            school=school, status=Student.Status.ACTIVE,
            enrollments__section__in=section_pks, enrollments__academic_year__is_current=True,
        ).distinct()
        reachable = {}
        for student_id, own_user in students.values_list("pk", "user_id"):
            reachable.setdefault(student_id, set()).add(own_user)
        for student_id, guardian_user in StudentGuardian.objects.filter(student__in=students.values("pk")).values_list(
            "student_id", "guardian__user_id"
        ):
            reachable.setdefault(student_id, set()).add(guardian_user)
        for users in reachable.values():
            users = (users - {None}) & eligible
            if users:
                candidates |= users
            else:
                unreached += 1

    candidates &= eligible
    candidates.discard(notice.author_id)
    read = NoticeRead.objects.filter(notice=notice, user_id__in=candidates).count()
    return Reach(total=len(candidates), read=read, unreached=unreached)
