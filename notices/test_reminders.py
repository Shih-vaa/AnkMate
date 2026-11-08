from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from academics.models import Enrollment
from accounts.models import Membership
from attendance import services as attendance_services
from core.models import School
from core.testing import WorldTestCase
from fees import services as fee_services
from fees.models import FeeInvoice
from people.models import Guardian, Student, StudentGuardian

from . import reminders, services
from .models import Reminder
from .reminders import ReminderError

TODAY = date(2026, 11, 4)  # a Wednesday, inside the fixture's 2026-27 academic year
OCT = date(2026, 10, 1)


class ReminderCase(WorldTestCase):
    """Fixture reminder (school A): p1 = parent of Asha + Chitra, p2 and 'dual' = parents of Bimal, Asha also has her own
    login (u_student), Dev (class 6) has no parent login at all. Class 5 pays Rs 1000 a month, class 6 Rs 2000."""

    def setUp(self):
        super().setUp()
        for target in ("fees.services.today", "attendance.services.today", "notices.services.today"):
            patcher = patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)

    def bill_october(self):
        """October's invoices were due on 10 Oct, so on the pinned 4 Nov everybody is overdue."""
        return fee_services.generate_invoices(self.school_a, self.year_a, OCT, self.u_accountant)

    def pay_in_full(self, student, month=OCT):
        invoice = FeeInvoice.objects.get(student=student, month=month, status="issued")
        fee_services.record_payment(self.school_a, student, invoice.total, "cash", TODAY, self.u_accountant)

    def mine(self, user):
        return list(Reminder.objects.filter(user=user).order_by("pk"))

    def set_settings(self, **values):
        School.objects.filter(pk=self.school_a.pk).update(settings=values)
        self.school_a.refresh_from_db()


class ParentLoginTests(ReminderCase):
    def test_parents_with_a_login_are_found_for_each_child(self):
        found = reminders.parent_logins(self.school_a, [self.asha.pk, self.bimal.pk, self.chitra.pk, self.dev.pk])
        self.assertEqual({u.username for u in found[self.asha.pk]}, {"p1"})
        self.assertEqual({u.username for u in found[self.chitra.pk]}, {"p1"})
        self.assertEqual({u.username for u in found[self.bimal.pk]}, {"p2", "dual"})
        self.assertEqual(found[self.dev.pk], [])

    def test_the_main_contact_comes_first(self):
        StudentGuardian.objects.filter(student=self.bimal, guardian__user=self.u_dual).update(is_primary=True)
        found = reminders.parent_logins(self.school_a, [self.bimal.pk])
        self.assertEqual([u.username for u in found[self.bimal.pk]], ["dual", "p2"])

    def test_a_guardian_who_cannot_sign_in_is_not_counted(self):
        self.u_p2.is_active = False
        self.u_p2.save()
        Membership.objects.filter(user=self.u_dual).update(is_active=False)
        self.assertEqual(reminders.parent_logins(self.school_a, [self.bimal.pk])[self.bimal.pk], [])

    def test_a_guardian_without_a_login_is_not_counted(self):
        nobody = Guardian.objects.create(school=self.school_a, first_name="No", phone="9000000001")
        StudentGuardian.objects.create(school=self.school_a, student=self.dev, guardian=nobody)
        self.assertEqual(reminders.parent_logins(self.school_a, [self.dev.pk])[self.dev.pk], [])

    def test_a_parent_login_from_another_school_is_ignored(self):
        self.assertEqual(reminders.parent_logins(self.school_b, [self.asha.pk])[self.asha.pk], [])


class CreateReminderTests(ReminderCase):
    def make(self, **kw):
        defaults = dict(school=self.school_a, user=self.u_p1, kind="fee", title="T", body="B")
        defaults.update(kw)
        school, user, kind, title, body = (defaults.pop(k) for k in ("school", "user", "kind", "title", "body"))
        return reminders.create_reminder(school, user, kind, title, body, **defaults)

    def test_without_a_key_every_call_makes_a_new_one(self):
        self.make()
        self.make()
        self.assertEqual(Reminder.objects.count(), 2)

    def test_with_a_key_the_same_reminder_is_made_once(self):
        first, created = self.make(key="k1")
        again, created_again = self.make(key="k1")
        self.assertEqual((created, created_again, first.pk), (True, False, again.pk))
        self.assertEqual(Reminder.objects.count(), 1)
        self.make(key="k1", user=self.u_p2)  # a different person is a different reminder
        self.assertEqual(Reminder.objects.count(), 2)

    def test_the_database_refuses_a_duplicate_key_for_one_person(self):
        self.make(key="k1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reminder.objects.create(school=self.school_a, user=self.u_p1, kind="fee", title="x", body="y", key="k1")
        Reminder.objects.create(school=self.school_a, user=self.u_p1, kind="fee", title="x", body="y", key="")
        Reminder.objects.create(school=self.school_a, user=self.u_p1, kind="fee", title="x", body="y", key="")  # blank keys may repeat

    def test_long_text_is_cut_to_fit(self):
        reminder, _ = self.make(title="t" * 300, body="b" * 900)
        self.assertEqual((len(reminder.title), len(reminder.body)), (120, 400))

    def test_a_student_of_another_school_is_refused(self):
        with self.assertRaises(ValidationError):
            self.make(student=self.zoya)

    def test_reading_marks_it_and_keeps_the_first_time(self):
        reminder, _ = self.make()
        self.assertFalse(reminder.is_read)
        reminders.mark_reminder_read(reminder)
        first = Reminder.objects.get(pk=reminder.pk).read_at
        self.assertIsNotNone(first)
        reminders.mark_reminder_read(reminder)
        self.assertEqual(Reminder.objects.get(pk=reminder.pk).read_at, first)

    def test_mark_all_read_touches_only_that_persons_reminders_in_that_school(self):
        self.make()
        self.make(key="a")
        other, _ = self.make(user=self.u_p2)
        self.assertEqual(reminders.mark_all_read(self.u_p1, self.school_a), 2)
        self.assertEqual(reminders.unread_reminders(self.u_p1, self.school_a).count(), 0)
        self.assertEqual(reminders.unread_reminders(self.u_p2, self.school_a).count(), 1)
        self.assertEqual(reminders.reminders_for(self.u_p1, self.school_b).count(), 0)
        self.assertIsNone(Reminder.objects.get(pk=other.pk).read_at)

    def test_safe_links(self):
        self.assertEqual(reminders.safe_link("/fees/student/1/"), "/fees/student/1/")
        for bad in ("", "https://evil.example/", "//evil.example/", "javascript:alert(1)", "/\\evil.example", "fees/student/1/"):
            self.assertEqual(reminders.safe_link(bad), "", bad)

    def test_the_badge_counts_new_notices_and_unopened_reminders(self):
        services.create_notice(self.school_a, self.u_office, title="N", body="b", everyone=True)
        self.make()
        self.make(key="x")
        self.assertEqual(reminders.unread_total(self.u_p1, self.school_a), 3)
        services.mark_read(services.visible_notices(self.u_p1, self.school_a).get(), self.u_p1)
        reminders.mark_all_read(self.u_p1, self.school_a)
        self.assertEqual(reminders.unread_total(self.u_p1, self.school_a), 0)
        self.assertEqual(reminders.unread_total(self.u_orphan, self.school_a), 0)
        self.assertEqual(reminders.unread_total(self.u_pb, self.school_a), 0)


class FeeReminderPlanTests(ReminderCase):
    def setUp(self):
        super().setUp()
        self.bill_october()

    def rows(self, **kw):
        return {r["student"].admission_no: r for r in reminders.fee_reminder_rows(self.school_a, **kw)}

    def test_every_overdue_family_is_listed_with_who_would_be_told(self):
        rows = self.rows()
        self.assertEqual(set(rows), {"A1", "A2", "A3", "A4"})
        self.assertEqual({u.username for u in rows["A2"]["parents"]}, {"p2", "dual"})
        self.assertEqual(rows["A1"]["state"], "ready")
        self.assertEqual(rows["A4"]["state"], "no_login")  # Dev: nobody can be told through the app
        self.assertEqual(rows["A4"]["overdue"], Decimal("2000.00"))
        self.assertEqual(rows["A1"]["class_label"], "Class 5-A")

    def test_a_family_that_has_paid_is_not_listed(self):
        self.pay_in_full(self.asha)
        self.assertNotIn("A1", self.rows())

    def test_fees_not_yet_due_are_not_overdue(self):
        fee_services.generate_invoices(self.school_a, self.year_a, date(2026, 11, 1), self.u_accountant)  # due 10 Nov
        for row in self.rows().values():
            self.assertEqual(row["overdue"], row["outstanding"])  # only October counts
        self.pay_in_full(self.bimal)
        self.assertNotIn("A2", self.rows())  # still owes November, but nothing is overdue

    def test_filter_by_class(self):
        rows = {r["student"].admission_no for r in reminders.fee_reminder_rows(self.school_a, grade_id=self.grade6.pk)}
        self.assertEqual(rows, {"A4"})

    def test_a_family_reminded_this_week_is_marked_recent_until_the_week_is_up(self):
        reminders.send_fee_reminders(self.school_a, [self.asha.pk], self.u_accountant)
        self.assertEqual(self.rows()["A1"]["state"], "recent")
        self.assertIsNotNone(self.rows()["A1"]["last_sent"])
        Reminder.objects.update(created_at=timezone.now() - timedelta(days=100))  # long ago in real time
        self.assertEqual(self.rows()["A1"]["state"], "ready")

    def test_other_schools_dues_never_appear(self):
        self.assertNotIn("B1", self.rows())
        self.assertEqual(reminders.fee_reminder_rows(self.school_b), [])


class SendFeeRemindersTests(ReminderCase):
    def setUp(self):
        super().setUp()
        self.bill_october()
        self.everyone = [self.asha.pk, self.bimal.pk, self.chitra.pk, self.dev.pk]

    def send(self, ids=None, sender=None):
        return reminders.send_fee_reminders(self.school_a, self.everyone if ids is None else ids, sender or self.u_accountant)

    def test_each_parent_login_gets_one_reminder_per_child(self):
        result = self.send()
        self.assertEqual((result.students, result.reminders), (3, 4))
        self.assertEqual(len(self.mine(self.u_p1)), 2)      # Asha and Chitra
        self.assertEqual(len(self.mine(self.u_p2)), 1)      # Bimal
        self.assertEqual(len(self.mine(self.u_dual)), 1)    # Bimal, through the other parent
        self.assertEqual(self.mine(self.u_student), [])     # the student is not chased about fees

    def test_the_wording_and_link(self):
        self.send([self.asha.pk])
        reminder = self.mine(self.u_p1)[0]
        self.assertEqual(reminder.kind, "fee")
        self.assertEqual(reminder.title, "Fee reminder for Asha")
        self.assertIn("₹1,000.00 of Asha Test's fees (Class 5-A) is overdue", reminder.body)
        self.assertIn("10 Oct 2026", reminder.body)
        self.assertEqual(reminder.link, reverse("fee_student", args=[self.asha.pk]))
        self.assertEqual((reminder.student, reminder.sent_by, reminder.school), (self.asha, self.u_accountant, self.school_a))
        self.assertIsNone(reminder.read_at)

    def test_a_family_with_no_login_is_reported_so_the_office_can_phone_them(self):
        result = self.send()
        self.assertEqual([s.admission_no for s in result.no_login], ["A4"])
        self.assertEqual(Reminder.objects.filter(student=self.dev).count(), 0)

    def test_sending_again_the_same_week_reminds_nobody_twice(self):
        self.send()
        again = self.send()
        self.assertEqual((again.students, again.reminders, again.recent), (0, 0, 3))
        self.assertEqual(Reminder.objects.count(), 4)

    def test_after_a_week_a_family_can_be_reminded_again(self):
        self.send()
        Reminder.objects.update(created_at=timezone.now() - timedelta(days=100))
        Reminder.objects.update(key="")  # the same-day guard is separate; see the next test
        result = self.send()
        self.assertEqual((result.students, result.reminders), (3, 4))

    def test_pressing_send_twice_on_the_same_day_cannot_double_up(self):
        self.send()
        Reminder.objects.update(created_at=timezone.now() - timedelta(days=100))  # pretend the week-long guard has passed
        result = self.send()
        self.assertEqual((result.students, result.reminders), (0, 0))
        self.assertEqual(Reminder.objects.count(), 4)

    def test_only_the_chosen_families_are_reminded(self):
        result = self.send([self.asha.pk])
        self.assertEqual((result.students, result.reminders), (1, 1))
        self.assertEqual(Reminder.objects.count(), 1)

    def test_a_family_that_paid_since_the_list_was_shown_is_left_alone(self):
        self.pay_in_full(self.chitra)
        result = self.send([self.asha.pk, self.chitra.pk])
        self.assertEqual((result.students, result.not_overdue), (1, 1))
        self.assertEqual(self.mine(self.u_p1)[0].student, self.asha)

    def test_a_student_id_from_nowhere_or_another_school_is_not_overdue_here(self):
        result = self.send([self.zoya.pk, 999999])
        self.assertEqual((result.students, result.not_overdue), (0, 2))
        self.assertEqual(Reminder.objects.count(), 0)

    def test_who_may_send(self):
        for user in (self.u_accountant, self.u_office, self.u_admin):
            self.assertTrue(reminders.can_send_fee_reminders(user, self.school_a), user.username)
        for user in (self.u_t1, self.u_p1, self.u_student, self.u_orphan, self.u_admin_b):
            self.assertFalse(reminders.can_send_fee_reminders(user, self.school_a), user.username)
            with self.assertRaises(ReminderError, msg=user.username):
                self.send(sender=user)
        self.assertEqual(Reminder.objects.count(), 0)

    def test_nothing_chosen_is_an_error(self):
        with self.assertRaises(ReminderError):
            self.send([])

    def test_a_deactivated_parent_login_is_skipped(self):
        self.u_p2.is_active = False
        self.u_p2.save()
        result = self.send([self.bimal.pk])
        self.assertEqual(result.reminders, 1)  # only 'dual'
        self.assertEqual(self.mine(self.u_p2), [])


class AbsenceAlertTests(ReminderCase):
    def mark(self, section, statuses, on_date=TODAY, user=None):
        return attendance_services.save_section_attendance(
            section, on_date, {s.pk: code for s, code in statuses.items()}, user or self.u_t1
        )

    def test_parents_are_told_when_their_child_is_marked_absent(self):
        self.mark(self.sec5a, {self.asha: "A", self.bimal: "A"})
        asha_alert = self.mine(self.u_p1)
        self.assertEqual(len(asha_alert), 1)
        self.assertEqual(asha_alert[0].kind, "absence")
        self.assertEqual(asha_alert[0].title, "Asha was absent on Wed 04 Nov")
        self.assertIn("Asha Test (Class 5-A) was marked absent on Wednesday 04 November", asha_alert[0].body)
        self.assertEqual(asha_alert[0].link, reverse("attendance_student", args=[self.asha.pk]))
        self.assertEqual((asha_alert[0].student, asha_alert[0].sent_by), (self.asha, self.u_t1))
        self.assertEqual(len(self.mine(self.u_p2)), 1)       # Bimal's parents both hear
        self.assertEqual(len(self.mine(self.u_dual)), 1)
        self.assertEqual(self.mine(self.u_student), [])      # the student is not messaged about their own absence

    def test_children_who_came_to_school_cause_no_alert(self):
        self.mark(self.sec5a, {self.asha: "P", self.bimal: "L"})
        self.assertEqual(Reminder.objects.count(), 0)

    def test_leave_and_half_days_are_not_absences(self):
        self.mark(self.sec5a, {self.asha: "E", self.bimal: "H"})
        self.assertEqual(Reminder.objects.count(), 0)

    def test_saving_the_same_register_again_does_not_repeat_the_alert(self):
        self.mark(self.sec5a, {self.asha: "A", self.bimal: "P"})
        self.mark(self.sec5a, {self.asha: "A", self.bimal: "P"})
        self.assertEqual(len(self.mine(self.u_p1)), 1)

    def test_a_correction_before_it_was_read_takes_the_alert_back(self):
        self.mark(self.sec5a, {self.asha: "A"})
        self.mark(self.sec5a, {self.asha: "P"})
        self.assertEqual(Reminder.objects.count(), 0)

    def test_a_correction_after_it_was_read_is_followed_by_a_note(self):
        self.mark(self.sec5a, {self.asha: "A"})
        reminders.mark_all_read(self.u_p1, self.school_a)
        self.mark(self.sec5a, {self.asha: "L"})
        kinds = [r.kind for r in self.mine(self.u_p1)]
        self.assertEqual(kinds, ["absence", "absence_fix"])
        fix = self.mine(self.u_p1)[1]
        self.assertEqual(fix.title, "Attendance corrected for Asha")
        self.assertIn("now marked late", fix.body)
        self.mark(self.sec5a, {self.asha: "P"})  # a second correction does not pile up notes
        self.assertEqual(len(self.mine(self.u_p1)), 2)

    def test_marked_absent_again_after_a_correction_alerts_afresh(self):
        self.mark(self.sec5a, {self.asha: "A"})
        reminders.mark_all_read(self.u_p1, self.school_a)
        self.mark(self.sec5a, {self.asha: "P"})
        self.mark(self.sec5a, {self.asha: "A"})
        found = self.mine(self.u_p1)
        self.assertEqual([r.kind for r in found], ["absence"])  # the "corrected" note no longer holds
        self.assertIsNone(found[0].read_at)

    def test_only_recent_days_trigger_alerts(self):
        self.mark(self.sec5a, {self.asha: "A"}, on_date=TODAY - timedelta(days=2))
        self.assertEqual(len(self.mine(self.u_p1)), 1)
        self.mark(self.sec5a, {self.bimal: "A"}, on_date=TODAY - timedelta(days=3))
        self.assertEqual(self.mine(self.u_p2), [])  # a back-filled week-old register does not alarm anyone

    def test_the_window_is_a_school_setting(self):
        self.set_settings(absence_alert_days=5)
        self.mark(self.sec5a, {self.bimal: "A"}, on_date=TODAY - timedelta(days=5))
        self.assertEqual(len(self.mine(self.u_p2)), 1)
        self.set_settings(absence_alert_days="nonsense")
        self.assertEqual(reminders.rules_for(self.school_a).absence_alert_days, 2)

    def test_a_school_can_switch_alerts_off(self):
        self.set_settings(absence_alerts=False)
        self.mark(self.sec5a, {self.asha: "A"})
        self.assertEqual(Reminder.objects.count(), 0)

    def test_an_absent_child_with_no_parent_login_is_simply_skipped(self):
        self.mark(self.sec6a, {self.dev: "A"}, user=self.u_dual)
        self.assertEqual(Reminder.objects.count(), 0)

    def test_alerts_do_not_cross_schools(self):
        self.mark(self.sec5a, {self.asha: "A"})
        self.assertEqual(Reminder.objects.exclude(school=self.school_a).count(), 0)
        self.assertEqual(self.mine(self.u_pb), [])

    def test_a_failure_while_alerting_never_loses_the_attendance(self):
        with patch("notices.reminders.absence_alerts", side_effect=RuntimeError("boom")):
            with self.assertLogs("attendance.services", level="ERROR"):
                self.mark(self.sec5a, {self.asha: "A"})
        from attendance.models import AttendanceRecord
        self.assertEqual(AttendanceRecord.objects.get(student=self.asha, date=TODAY).status, "A")
        self.assertEqual(Reminder.objects.count(), 0)

    def test_the_marking_screen_sends_the_alerts(self):
        self.client.force_login(self.u_t1)
        response = self.client.post(
            reverse("attendance_mark", args=[self.sec5a.pk]),
            {"date": TODAY.isoformat(), f"status_{self.asha.pk}": "A", f"status_{self.bimal.pk}": "P"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.mine(self.u_p1)), 1)
        self.assertEqual(self.mine(self.u_p2), [])

    def test_the_alert_is_connected_exactly_once(self):
        from attendance.signals import attendance_saved
        ours = [r for r in attendance_saved.receivers if r[0][0] == "notices-absence-alerts"]
        self.assertEqual(len(ours), 1)
        self.mark(self.sec5a, {self.asha: "A"})
        self.assertEqual(len(self.mine(self.u_p1)), 1)

    def test_a_class_register_costs_a_fixed_number_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        extra = [
            Student.objects.create(school=self.school_a, admission_no=f"X{n}", first_name=f"Kid{n}") for n in range(30)
        ]
        for n, kid in enumerate(extra):
            Enrollment.objects.create(school=self.school_a, student=kid, section=self.sec5a, roll_no=10 + n)
        with CaptureQueriesContext(connection) as small:
            self.mark(self.sec5a, {self.asha: "A"})
        with CaptureQueriesContext(connection) as big:
            self.mark(self.sec5a, {kid: "P" for kid in extra} | {self.asha: "A", self.bimal: "A"})
        self.assertLessEqual(len(big), len(small) + 6)
