"""Fake notices and fee reminders for the demo school (never run against real data)."""
import random
from datetime import timedelta

from django.utils import timezone

from academics.models import Grade, Section
from accounts.models import Membership, Role

from . import reminders, services
from .models import Notice, NoticeRead, Reminder


def clear_notices(school):
    """Delete the school's notices (with their audiences and read marks) and reminders. Demo data only."""
    Reminder.objects.filter(school=school).delete()
    Notice.objects.filter(school=school).delete()


def _login_with_role(school, role_code):
    membership = (
        Membership.objects.filter(school=school, role__code=role_code, is_active=True).select_related("user").order_by("pk").first()
    )
    return membership.user if membership else None


def seed_notices(school, *, seed=5, reset=False):
    """Returns {'notices': n, 'reads': n, 'reminders': n}."""
    if Notice.objects.filter(school=school).exists():
        if not reset:
            raise ValueError("Notices already exist. Pass reset=True to replace them.")
        clear_notices(school)
    principal, office = _login_with_role(school, "admin"), _login_with_role(school, "office")
    if principal is None:
        raise ValueError("The school has no principal login yet. Run seed_demo first.")

    rng = random.Random(seed)
    today = services.today()
    roles = {r.code: r for r in Role.objects.filter(school=school)}
    senior_grades = list(Grade.objects.filter(school=school).order_by("order"))[-3:]
    section = (
        Section.objects.filter(school=school, academic_year__is_current=True, class_teacher__user__isnull=False)
        .select_related("grade", "class_teacher__user").order_by("grade__order", "name").first()
    )

    plan = [
        (principal, 6, "Annual Day on 20 December", "event",
         "Our Annual Day will be held on 20 December at 10 am in the school hall. All parents are warmly invited.\n\n"
         "Students taking part should report by 8:30 am in full uniform.", {"everyone": True}, today + timedelta(days=45)),
        (principal, 4, "Winter break", "holiday",
         "The school will remain closed from 24 December to 1 January. Classes resume on 2 January.", {"everyone": True}, today + timedelta(days=50)),
        (principal, 3, "Half-yearly exam timetable", "exam",
         "The half-yearly examinations begin next week. The timetable is pinned in each classroom. Please make sure your child "
         "carries the admit card every day.", {"grade_ids": [g.pk for g in senior_grades]}, None),
        (principal, 2, "Fees for this month", "fees",
         "This month's fees are due by the 10th. You can see what is due for each child under Fees. Please pay at the school office "
         "between 9 am and 1 pm.", {"role_ids": [roles["parent"].pk]}, None),
        (office or principal, 1, "Staff meeting on Monday", "general",
         "All teachers are requested to attend a short staff meeting on Monday at 8 am in the staff room.",
         {"role_ids": [roles["teacher"].pk]}, today + timedelta(days=7)),
    ]
    if section is not None:
        plan.append(
            (section.class_teacher.user, 0, f"Parent-teacher meeting, {section.grade.name}-{section.name}", "event",
             "We will meet this Saturday between 10 am and 12 noon to talk about your child's progress. "
             "Please bring the report card.", {"section_ids": [section.pk]}, today + timedelta(days=5))
        )

    notices = []
    for author, days_ago, title, category, body, audience, expires_on in plan:
        notice = services.create_notice(school, author, title=title, body=body, category=category, expires_on=expires_on, **audience)
        Notice.objects.filter(pk=notice.pk).update(posted_at=timezone.now() - timedelta(days=days_ago, hours=rng.randint(0, 5)))
        notices.append(notice)

    # Some people have already read some of them.
    people = sorted(services.eligible_user_ids(school))
    reads = [
        NoticeRead(school=school, notice=notice, user_id=uid)
        for notice in notices[:-1]
        for uid in people
        if rng.random() < 0.4
    ]
    NoticeRead.objects.bulk_create(reads, ignore_conflicts=True)

    # A fee reminder round for the families who are overdue.
    sent = 0
    try:
        ready = [row["student"].pk for row in reminders.fee_reminder_rows(school) if row["state"] == "ready"][:15]
        if ready:
            sent = reminders.send_fee_reminders(school, ready, principal).reminders
    except reminders.ReminderError:
        pass
    return {"notices": len(notices), "reads": len(reads), "reminders": sent}
