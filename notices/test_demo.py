from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from accounts.models import Membership
from core.models import School

from .demo import clear_notices, seed_notices
from .models import Notice, NoticeRead, Reminder
from .test_reminders import ReminderCase


class SeedNoticesTests(ReminderCase):
    def test_it_creates_notices_for_different_audiences(self):
        counts = seed_notices(self.school_a)
        self.assertEqual(counts["notices"], 6)
        self.assertEqual(Notice.objects.filter(school=self.school_a).count(), 6)
        labels = {tuple(n.audience_labels()) for n in Notice.objects.all()}
        self.assertIn(("Everyone",), labels)
        self.assertIn(("Parent / Guardian",), labels)
        self.assertIn(("Teacher",), labels)
        self.assertIn(("Class 5-A",), labels)  # the class teacher's own section
        self.assertTrue(NoticeRead.objects.exists())

    def test_the_class_teacher_posts_the_meeting_notice_to_their_own_section(self):
        seed_notices(self.school_a)
        meeting = Notice.objects.get(title__startswith="Parent-teacher meeting")
        self.assertEqual(meeting.author, self.u_t1)

    def test_overdue_families_get_fee_reminders(self):
        self.bill_october()
        counts = seed_notices(self.school_a)
        self.assertEqual(counts["reminders"], Reminder.objects.filter(school=self.school_a).count())
        self.assertGreater(counts["reminders"], 0)

    def test_without_any_dues_no_reminders_are_made(self):
        self.assertEqual(seed_notices(self.school_a)["reminders"], 0)

    def test_it_will_not_run_twice_unless_asked_to_start_over(self):
        seed_notices(self.school_a)
        with self.assertRaises(ValueError):
            seed_notices(self.school_a)
        self.assertEqual(seed_notices(self.school_a, reset=True)["notices"], 6)
        self.assertEqual(Notice.objects.count(), 6)

    def test_it_needs_a_principal(self):
        Membership.objects.filter(role__code="admin", role__school=self.school_a).delete()
        with self.assertRaises(ValueError):
            seed_notices(self.school_a)

    def test_it_leaves_other_schools_alone(self):
        from . import services
        services.create_notice(self.school_b, self.u_admin_b, title="Beta", body="x", everyone=True)
        seed_notices(self.school_a)
        clear_notices(self.school_a)
        self.assertEqual(list(Notice.objects.values_list("title", flat=True)), ["Beta"])

    def test_the_command(self):
        out = StringIO()
        with override_settings(DEBUG=True):
            call_command("seed_notices", school="alpha", stdout=out)
        self.assertIn("Created 6 notices", out.getvalue())
        with override_settings(DEBUG=True), self.assertRaises(CommandError):
            call_command("seed_notices", school="alpha")  # already there
        with override_settings(DEBUG=True):
            call_command("seed_notices", school="alpha", reset=True, stdout=StringIO())
        with override_settings(DEBUG=True), self.assertRaises(CommandError):
            call_command("seed_notices", school="no-such-school")
        with override_settings(DEBUG=False), self.assertRaises(CommandError):
            call_command("seed_notices", school="alpha", reset=True)
        self.assertEqual(School.objects.filter(code="alpha").count(), 1)
