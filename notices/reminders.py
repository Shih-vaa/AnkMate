"""Reminders: messages to one person at a time.

Two kinds are made by the system for parents:

* Fee reminders. The office looks at who is overdue, ticks the families to remind and sends. Each family is
  reminded at most once a week, and families with no parent login are listed so the office can phone them.
* Absence alerts. When a teacher saves attendance and a child is marked absent, the parents hear about it that
  day. If the mark is corrected, an alert nobody has read yet is withdrawn, and one already read is followed by a correction.

Per-school switches live in `School.settings`:

    absence_alerts        true/false: tell parents when their child is marked absent        default true
    absence_alert_days    only for days this recent (a late back-fill must not alarm)        default 2
"""
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from attendance import services as attendance_services
from attendance.models import Status
from accounts.access import has_perm
from core.money import format_inr
from fees import services as fee_services
from people.models import Student, StudentGuardian

from . import services
from .models import Reminder

FEE_COOLDOWN_DAYS = 7  # a family is reminded about fees at most once in this many days


class ReminderError(ValueError):
    """A rule was broken; the message is written to be shown to the person who tried."""


# -- school rules ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rules:
    absence_alerts: bool
    absence_alert_days: int


def rules_for(school):
    cfg = school.settings or {}
    try:
        days = max(0, int(cfg.get("absence_alert_days", 2)))
    except (TypeError, ValueError):
        days = 2
    return Rules(absence_alerts=cfg.get("absence_alerts", True) is not False, absence_alert_days=days)


# -- the building blocks -----------------------------------------------------------------------

def parent_logins(school, student_ids):
    """{student_id: [User, ...]}: each student's parents or guardians who can sign in and read notices, main contact first."""
    student_ids = list(student_ids)
    eligible = services.eligible_user_ids(school)
    result = {sid: [] for sid in student_ids}
    seen = set()
    links = (
        StudentGuardian.objects.filter(school=school, student_id__in=student_ids, guardian__user__isnull=False)
        .select_related("guardian__user")
        .order_by("-is_primary", "guardian__first_name", "pk")
    )
    for link in links:
        user = link.guardian.user
        if user.pk in eligible and (link.student_id, user.pk) not in seen:
            seen.add((link.student_id, user.pk))
            result[link.student_id].append(user)
    return result


def create_reminder(school, user, kind, title, body, *, student=None, link="", key="", sent_by=None):
    """Returns (reminder, created). With a `key`, asking twice for the same reminder gives back the first one."""
    fields = {"kind": kind, "title": title[:120], "body": body[:400], "student": student, "link": link, "sent_by": sent_by}
    if key:
        return Reminder.objects.get_or_create(school=school, user=user, key=key, defaults=fields)
    return Reminder.objects.create(school=school, user=user, **fields), True


def reminders_for(user, school):
    return Reminder.objects.filter(school=school, user=user)


def unread_reminders(user, school):
    return reminders_for(user, school).filter(read_at__isnull=True)


def mark_reminder_read(reminder):
    if reminder.read_at is None:
        reminder.read_at = timezone.now()
        reminder.save(update_fields=["read_at"])


def mark_all_read(user, school):
    return unread_reminders(user, school).update(read_at=timezone.now())


def unread_total(user, school):
    """What the badge in the navigation shows: new notices plus unopened reminders."""
    if not services.can_view(user, school):
        return 0
    return services.unread_notices(user, school).count() + unread_reminders(user, school).count()


def safe_link(link):
    """Reminders only ever point at pages of this app."""
    return link if link.startswith("/") and not link.startswith("//") and "\\" not in link else ""


# -- fee reminders -----------------------------------------------------------------------------

def can_send_fee_reminders(user, school):
    return has_perm(user, school, "fees.collect", all_scope=True)


def fee_reminder_rows(school, *, grade_id=None, now=None):
    """Students with overdue fees, each with who would be told and whether they were reminded lately.
    `state` is 'ready', 'recent' (reminded within the last week) or 'no_login' (no parent can sign in)."""
    now = now or fee_services.today()
    rows = fee_services.dues_report(school, grade_id=grade_id, overdue_only=True, now=now)
    ids = [row["student"].pk for row in rows]
    parents = parent_logins(school, ids)
    last = dict(
        Reminder.objects.filter(school=school, kind=Reminder.Kind.FEE, student_id__in=ids)
        .values("student_id")
        .annotate(last=Max("created_at"))
        .values_list("student_id", "last")
    )
    for row in rows:
        sid = row["student"].pk
        row["parents"] = parents[sid]
        row["last_sent"] = last.get(sid)
        if not row["parents"]:
            row["state"] = "no_login"
        elif row["last_sent"] and (now - timezone.localtime(row["last_sent"]).date()).days < FEE_COOLDOWN_DAYS:
            row["state"] = "recent"
        else:
            row["state"] = "ready"
    return rows


@dataclass
class FeeSendResult:
    students: int = 0       # families reminded
    reminders: int = 0      # messages sent (one per parent login)
    recent: int = 0         # skipped: reminded in the last week
    not_overdue: int = 0    # skipped: paid up since the list was shown
    no_login: list = field(default_factory=list)  # students with no parent login: phone these families


def send_fee_reminders(school, student_ids, sender, *, now=None):
    """Remind the parents of the chosen students about overdue fees. Dues are worked out again here, so a family that has
    paid since the list was shown is left alone, and nobody is reminded twice in a week."""
    if not can_send_fee_reminders(sender, school):
        raise ReminderError("You don't have permission to send fee reminders.")
    now = now or fee_services.today()
    wanted = {int(i) for i in student_ids}
    if not wanted:
        raise ReminderError("Please choose at least one family to remind.")
    rows = {row["student"].pk: row for row in fee_reminder_rows(school, now=now)}
    result = FeeSendResult(not_overdue=len(wanted - set(rows)))
    for sid in sorted(wanted & set(rows)):
        row = rows[sid]
        if row["state"] == "no_login":
            result.no_login.append(row["student"])
            continue
        if row["state"] == "recent":
            result.recent += 1
            continue
        student = row["student"]
        body = (
            f"{format_inr(row['overdue'], 2)} of {student.full_name}'s fees ({row['class_label']}) is overdue. "
            f"The oldest due date was {row['oldest_due']:%d %b %Y}. Please pay at the school office."
        )
        made = 0
        with transaction.atomic():
            for user in row["parents"]:
                _, created = create_reminder(
                    school, user, Reminder.Kind.FEE, f"Fee reminder for {student.first_name}", body,
                    student=student, link=reverse("fee_student", args=[sid]),
                    key=f"fee:{sid}:{now.isoformat()}", sent_by=sender,
                )
                made += created
        if made:
            result.students += 1
            result.reminders += made
    return result


# -- absence alerts ----------------------------------------------------------------------------

def absence_alerts(section, on_date, marks, sent_by, *, now=None):
    """Tell the parents of children marked absent; take back or correct alerts for children whose mark changed.
    `marks` = {student_id: status code}. Returns the number of new alerts."""
    school = section.school
    rules = rules_for(school)
    now = now or attendance_services.today()
    if not rules.absence_alerts or not 0 <= (now - on_date).days <= rules.absence_alert_days:
        return 0

    students = {s.pk: s for s in Student.objects.filter(school=school, pk__in=list(marks))}
    parents = parent_logins(school, students)
    label = f"{section.grade.name}-{section.name}"
    day = on_date.isoformat()
    absent_key = lambda sid: f"absent:{sid}:{day}"
    fix_key = lambda sid: f"absent-fix:{sid}:{day}"
    sent = 0
    not_absent = [sid for sid, status in marks.items() if status != Status.ABSENT and sid in students]
    earlier = {}  # alerts already sent for children whose mark is not absent now, found in one query
    for reminder in Reminder.objects.filter(school=school, key__in=[absent_key(sid) for sid in not_absent]):
        earlier.setdefault(reminder.student_id, []).append(reminder)

    with transaction.atomic():
        for sid, status in marks.items():
            student = students.get(sid)
            if student is None:
                continue
            if status == Status.ABSENT:
                for user in parents[sid]:
                    reminder, created = create_reminder(
                        school, user, Reminder.Kind.ABSENCE, f"{student.first_name} was absent on {on_date:%a %d %b}",
                        f"{student.full_name} ({label}) was marked absent on {on_date:%A %d %B}. "
                        "If this is a mistake, or they were unwell, please let the school office know.",
                        student=student, link=reverse("attendance_student", args=[sid]), key=absent_key(sid), sent_by=sent_by,
                    )
                    if not created and reminder.read_at is not None:
                        # Marked absent again after a correction: the earlier "corrected" note no longer holds.
                        Reminder.objects.filter(school=school, user=user, key=fix_key(sid)).delete()
                        reminder.read_at = None
                        reminder.save(update_fields=["read_at"])
                        created = True
                    sent += created
                continue

            # Not absent (any more): take back an unread alert, or follow a read one with a correction.
            for alert in earlier.get(sid, []):
                if alert.read_at is None:
                    alert.delete()
                    continue
                create_reminder(
                    school, alert.user, Reminder.Kind.ABSENCE_FIX, f"Attendance corrected for {student.first_name}",
                    f"{student.first_name}'s attendance for {on_date:%A %d %B} was corrected: "
                    f"now marked {Status(status).label.lower()}.",
                    student=student, link=reverse("attendance_student", args=[sid]), key=fix_key(sid), sent_by=sent_by,
                )
    return sent
