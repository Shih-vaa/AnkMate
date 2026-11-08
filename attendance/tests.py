from datetime import date, timedelta
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from academics.models import AcademicYear, Enrollment
from accounts.access import visible_sections
from core.models import School
from core.testing import WorldTestCase
from people.models import Student

from . import services
from .models import AttendanceRecord, Holiday, Status
from .signals import attendance_saved

TODAY = date(2026, 11, 4)  # a Wednesday, inside the fixture's 2026-27 academic year
SUNDAY = date(2026, 11, 1)


class ClockMixin:
    """Pin 'today' so tests don't depend on when they run."""

    def setUp(self):
        super().setUp()
        patcher = patch("attendance.services.today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_settings(self, **values):
        School.objects.filter(pk=self.school_a.pk).update(settings=values)

    def school(self):
        return School.objects.get(pk=self.school_a.pk)

    def mark(self, section, on_date, statuses, *, user=None):
        """Directly store attendance, bypassing the web layer."""
        return services.save_section_attendance(section, on_date, {s.pk: st for s, st in statuses.items()}, user or self.u_admin)

    def post_attendance(self, user, section, on_date, statuses, **extra):
        self.client.force_login(user)
        data = {"date": on_date.isoformat(), **{f"status_{s.pk}": st for s, st in statuses.items()}, **extra}
        return self.client.post(reverse("attendance_mark", args=[section.pk]), data, follow=True)


class FixtureSanityTests(ClockMixin, WorldTestCase):
    def test_pinned_dates_are_the_weekdays_the_tests_assume(self):
        self.assertEqual(TODAY.weekday(), 2)  # Wednesday
        self.assertEqual(SUNDAY.weekday(), 6)


class SchoolRulesTests(ClockMixin, WorldTestCase):
    def test_defaults(self):
        rules = services.rules_for(self.school())
        self.assertEqual(rules.weekly_off, frozenset({6}))
        self.assertEqual(rules.marked_by, "class_teacher")
        self.assertEqual(rules.edit_window_days, 7)

    def test_school_can_override(self):
        self.set_settings(weekly_off=[5, 6], attendance_marked_by="assigned_teachers", attendance_edit_window_days="3")
        rules = services.rules_for(self.school())
        self.assertEqual(rules.weekly_off, frozenset({5, 6}))
        self.assertEqual(rules.marked_by, "assigned_teachers")
        self.assertEqual(rules.edit_window_days, 3)

    def test_junk_settings_fall_back_safely(self):
        self.set_settings(weekly_off=["x", 9, None], attendance_marked_by="nonsense", attendance_edit_window_days=-5)
        rules = services.rules_for(self.school())
        self.assertEqual(rules.weekly_off, frozenset())
        self.assertEqual(rules.marked_by, "class_teacher")
        self.assertEqual(rules.edit_window_days, 0)

    def test_closed_days(self):
        school = self.school()
        self.assertIsNone(services.non_working_reason(school, TODAY))
        self.assertEqual(services.non_working_reason(school, SUNDAY), "Weekly off")
        Holiday.objects.create(school=school, date=TODAY, name="Founders Day")
        self.assertEqual(services.non_working_reason(school, TODAY), "Founders Day")
        Holiday.objects.create(school=school, date=SUNDAY, name="Diwali")
        self.assertEqual(services.non_working_reason(school, SUNDAY), "Diwali")  # a named holiday wins

    def test_saturday_can_be_a_working_day_or_not(self):
        saturday = date(2026, 10, 31)
        self.assertIsNone(services.non_working_reason(self.school(), saturday))
        self.set_settings(weekly_off=[5, 6])
        self.assertEqual(services.non_working_reason(self.school(), saturday), "Weekly off")

    def test_holidays_of_another_school_do_not_apply(self):
        Holiday.objects.create(school=self.school_b, date=TODAY, name="Beta only")
        self.assertIsNone(services.non_working_reason(self.school(), TODAY))


class WhoMayMarkTests(ClockMixin, WorldTestCase):
    def ids(self, user):
        # Load the school fresh, as each web request does, so changed settings are picked up.
        return set(services.markable_sections(user, self.school()).values_list("pk", flat=True))

    def test_admin_may_mark_every_class(self):
        self.assertEqual(self.ids(self.u_admin), {self.sec5a.pk, self.sec5b.pk, self.sec6a.pk})

    def test_class_teacher_marks_only_their_own_class(self):
        self.assertEqual(self.ids(self.u_t1), {self.sec5a.pk})

    def test_subject_teachers_cannot_mark_by_default(self):
        self.assertEqual(self.ids(self.u_t2), set())
        self.assertEqual(self.ids(self.u_dual), set())

    def test_school_can_let_subject_teachers_mark(self):
        self.set_settings(attendance_marked_by="assigned_teachers")
        self.assertEqual(self.ids(self.u_t2), {self.sec5b.pk})
        self.assertEqual(self.ids(self.u_dual), {self.sec6a.pk})
        self.assertEqual(self.ids(self.u_t3), set())  # still nothing for a teacher with no classes

    def test_parents_students_and_other_schools_may_not(self):
        for user in (self.u_p1, self.u_student, self.u_admin_b, self.u_accountant):
            self.assertEqual(self.ids(user), set(), user.username)

    def test_can_mark_today_for_own_class(self):
        self.assertEqual(services.can_mark(self.u_t1, self.school_a, self.sec5a, TODAY), (True, ""))

    def test_cannot_mark_someone_elses_class(self):
        ok, reason = services.can_mark(self.u_t1, self.school_a, self.sec5b, TODAY)
        self.assertFalse(ok)
        self.assertIn("class's teacher", reason)

    def test_parent_gets_a_permission_message(self):
        ok, reason = services.can_mark(self.u_p1, self.school_a, self.sec5a, TODAY)
        self.assertFalse(ok)
        self.assertIn("permission", reason)

    def test_teacher_edit_window(self):
        self.assertTrue(services.can_mark(self.u_t1, self.school_a, self.sec5a, TODAY - timedelta(days=7))[0])
        ok, reason = services.can_mark(self.u_t1, self.school_a, self.sec5a, TODAY - timedelta(days=8))
        self.assertFalse(ok)
        self.assertIn("7 days", reason)

    def test_window_is_a_school_setting(self):
        self.set_settings(attendance_edit_window_days=30)
        self.assertTrue(services.can_mark(self.u_t1, self.school(), self.sec5a, TODAY - timedelta(days=30))[0])

    def test_admin_ignores_the_edit_window(self):
        self.assertTrue(services.can_mark(self.u_admin, self.school_a, self.sec5a, TODAY - timedelta(days=90))[0])

    def test_nobody_marks_the_future_or_outside_the_year(self):
        for user in (self.u_t1, self.u_admin):
            self.assertFalse(services.can_mark(user, self.school_a, self.sec5a, TODAY + timedelta(days=1))[0])
            self.assertFalse(services.can_mark(user, self.school_a, self.sec5a, date(2026, 3, 31))[0])
        later = TODAY.replace(year=2027, month=4, day=2)
        self.assertFalse(services.can_mark(self.u_admin, self.school_a, self.sec5a, later, now=later)[0])


class SavingTests(ClockMixin, WorldTestCase):
    def test_saves_and_records_who_marked(self):
        n = self.mark(self.sec5a, TODAY, {self.asha: "A", self.bimal: "P"}, user=self.u_t1)
        self.assertEqual(n, 2)
        record = AttendanceRecord.objects.get(student=self.asha, date=TODAY)
        self.assertEqual((record.status, record.section, record.marked_by, record.school), ("A", self.sec5a, self.u_t1, self.school_a))

    def test_saving_again_updates_instead_of_duplicating(self):
        self.mark(self.sec5a, TODAY, {self.asha: "A", self.bimal: "P"})
        self.mark(self.sec5a, TODAY, {self.asha: "L", self.bimal: "P"})
        self.assertEqual(AttendanceRecord.objects.filter(date=TODAY).count(), 2)
        self.assertEqual(AttendanceRecord.objects.get(student=self.asha, date=TODAY).status, "L")

    def test_a_partial_save_leaves_other_students_alone(self):
        self.mark(self.sec5a, TODAY, {self.asha: "A", self.bimal: "A"})
        self.mark(self.sec5a, TODAY, {self.asha: "P"})
        self.assertEqual(AttendanceRecord.objects.get(student=self.bimal, date=TODAY).status, "A")

    def test_students_from_other_sections_are_refused(self):
        with self.assertRaises(services.AttendanceError):
            self.mark(self.sec5a, TODAY, {self.asha: "P", self.chitra: "P"})
        self.assertEqual(AttendanceRecord.objects.count(), 0)  # nothing half-saved

    def test_unknown_status_and_empty_input_are_refused(self):
        with self.assertRaises(services.AttendanceError):
            services.save_section_attendance(self.sec5a, TODAY, {self.asha.pk: "Z"}, self.u_t1)
        with self.assertRaises(services.AttendanceError):
            services.save_section_attendance(self.sec5a, TODAY, {}, self.u_t1)

    def test_inactive_students_are_not_on_the_roster(self):
        Student.objects.filter(pk=self.bimal.pk).update(status="transferred")
        self.assertEqual([e.student_id for e in services.roster(self.sec5a)], [self.asha.pk])

    def test_record_keeps_the_section_the_student_was_in_that_day(self):
        self.mark(self.sec5a, TODAY - timedelta(days=1), {self.asha: "P"})
        Enrollment.objects.filter(student=self.asha).update(section=self.sec5b)
        self.assertEqual(AttendanceRecord.objects.get(student=self.asha).section, self.sec5a)


class SavedSignalTests(ClockMixin, WorldTestCase):
    """Other parts of the system can listen for saved attendance, but can never stop it being saved."""

    def listen(self, handler):
        attendance_saved.connect(handler, weak=False, dispatch_uid="test-listener")
        self.addCleanup(attendance_saved.disconnect, dispatch_uid="test-listener")

    def test_listeners_are_told_what_was_saved(self):
        calls = []
        self.listen(lambda sender, **kw: calls.append(kw))
        self.mark(self.sec5a, TODAY, {self.asha: "A", self.bimal: "P"}, user=self.u_t1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["section"], self.sec5a)
        self.assertEqual(calls[0]["on_date"], TODAY)
        self.assertEqual(calls[0]["marks"], {self.asha.pk: "A", self.bimal.pk: "P"})
        self.assertEqual(calls[0]["user"], self.u_t1)

    def test_nothing_is_sent_when_the_save_is_refused(self):
        calls = []
        self.listen(lambda sender, **kw: calls.append(kw))
        with self.assertRaises(services.AttendanceError):
            self.mark(self.sec5a, TODAY, {self.asha: "P", self.chitra: "P"})
        self.assertEqual(calls, [])

    def test_a_listener_that_fails_cannot_undo_the_attendance(self):
        def broken(sender, **kw):
            raise RuntimeError("listener bug")

        self.listen(broken)
        with self.assertLogs("attendance.services", level="ERROR"):
            n = self.mark(self.sec5a, TODAY, {self.asha: "A"})
        self.assertEqual(n, 1)
        self.assertEqual(AttendanceRecord.objects.get(student=self.asha, date=TODAY).status, "A")


class SummaryTests(ClockMixin, WorldTestCase):
    def test_percentage_weights(self):
        summary = services.summarize(["P", "P", "A", "L", "H", "E"])
        self.assertEqual(summary["marked"], 6)
        self.assertEqual(summary["attended"], 3.5)
        self.assertEqual(summary["percentage"], 58.3)
        self.assertEqual(summary["counts"], {"P": 2, "A": 1, "L": 1, "H": 1, "E": 1})

    def test_no_data_is_not_zero_percent(self):
        self.assertIsNone(services.summarize([])["percentage"])

    def test_percentages_for_many_students_at_once(self):
        d1, d2 = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
        self.mark(self.sec5a, d1, {self.asha: "P", self.bimal: "A"})
        self.mark(self.sec5a, d2, {self.asha: "A", self.bimal: "A"})
        result = services.percentages_for([self.asha.pk, self.bimal.pk, self.chitra.pk], d1, d2)
        self.assertEqual(result[self.asha.pk]["percentage"], 50.0)
        self.assertEqual(result[self.bimal.pk]["percentage"], 0.0)
        self.assertIsNone(result[self.chitra.pk]["percentage"])

    def test_day_overview_and_school_summary(self):
        self.mark(self.sec5a, TODAY, {self.asha: "P", self.bimal: "A"})
        stats = services.day_overview([self.sec5a.pk, self.sec5b.pk], TODAY)
        self.assertEqual(stats[self.sec5a.pk], {"total": 2, "marked": 2, "present": 1})
        self.assertEqual(stats[self.sec5b.pk], {"total": 1, "marked": 0, "present": 0})
        summary = services.school_day_summary(self.school_a, TODAY)
        self.assertEqual((summary["total"], summary["marked"], summary["present"]), (4, 2, 1))
        self.assertEqual((summary["sections"], summary["sections_marked"]), (3, 1))

    def test_other_schools_do_not_leak_into_the_summary(self):
        self.mark(self.sec5a, TODAY, {self.asha: "P"})
        self.assertEqual(services.school_day_summary(self.school_b, TODAY)["marked"], 0)

    def test_month_helpers(self):
        self.assertEqual(services.parse_month("2026-11", (1, 1)), (2026, 11))
        for junk in ("", "abc", "2026-13", "2026", None, "2026-00"):
            self.assertEqual(services.parse_month(junk, (2026, 11)), (2026, 11), junk)
        self.assertEqual(services.shift_month(2026, 12, 1), (2027, 1))
        self.assertEqual(services.shift_month(2026, 1, -1), (2025, 12))

    def test_month_selection_stays_inside_the_academic_year(self):
        year = self.year_a  # Apr 2026 - Mar 2027, "now" is Nov 2026
        chosen, prev_key, next_key = services.resolve_month("2026-09", year, TODAY)
        self.assertEqual((chosen, prev_key, next_key), ((2026, 9), "2026-08", "2026-10"))
        self.assertEqual(services.resolve_month("2027-02", year, TODAY)[0], (2026, 11))  # future month: clamp
        self.assertEqual(services.resolve_month("2020-01", year, TODAY)[0], (2026, 11))  # before the year: clamp
        chosen, prev_key, next_key = services.resolve_month("2026-04", year, TODAY)
        self.assertIsNone(prev_key)
        self.assertIsNone(services.resolve_month(None, year, TODAY)[2])  # current month has no "next"


class VisibleSectionsTests(ClockMixin, WorldTestCase):
    def ids(self, user):
        return set(visible_sections(user, self.school_a, "attendance.view").values_list("pk", flat=True))

    def test_admin_sees_all_and_teachers_see_what_they_teach(self):
        self.assertEqual(self.ids(self.u_admin), {self.sec5a.pk, self.sec5b.pk, self.sec6a.pk})
        self.assertEqual(self.ids(self.u_t1), {self.sec5a.pk})
        self.assertEqual(self.ids(self.u_t2), {self.sec5b.pk})  # subject teachers can view the register
        self.assertEqual(self.ids(self.u_dual), {self.sec6a.pk})

    def test_parents_and_students_get_no_class_level_data(self):
        self.assertEqual(self.ids(self.u_p1), set())
        self.assertEqual(self.ids(self.u_student), set())

    def test_other_schools_are_invisible(self):
        self.assertEqual(set(visible_sections(self.u_admin_b, self.school_a, "attendance.view")), set())


class AnonymousTests(ClockMixin, WorldTestCase):
    def test_everything_requires_login(self):
        urls = [
            reverse("attendance_home"),
            reverse("attendance_mark", args=[self.sec5a.pk]),
            reverse("attendance_register", args=[self.sec5a.pk]),
            reverse("attendance_student", args=[self.asha.pk]),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn(reverse("login"), response["Location"])


class MarkingScreenTests(ClockMixin, WorldTestCase):
    def url(self, section, on_date=None):
        base = reverse("attendance_mark", args=[section.pk])
        return f"{base}?date={on_date.isoformat()}" if on_date else base

    def get(self, user, section, on_date=None):
        self.client.force_login(user)
        return self.client.get(self.url(section, on_date))

    def test_class_teacher_sees_the_roster_everyone_present_by_default(self):
        response = self.get(self.u_t1, self.sec5a)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["student"].first_name for r in response.context["rows"]], ["Asha", "Bimal"])
        self.assertEqual({r["current"] for r in response.context["rows"]}, {"P"})
        self.assertTrue(response.context["editable"])
        self.assertContains(response, "Not taken yet")

    def test_other_classes_are_not_found_for_a_teacher(self):
        self.assertEqual(self.get(self.u_t1, self.sec5b).status_code, 404)
        self.assertEqual(self.get(self.u_t1, self.sec6a).status_code, 404)

    def test_other_schools_class_is_not_found(self):
        self.assertEqual(self.get(self.u_admin_b, self.sec5a).status_code, 404)
        self.assertEqual(self.get(self.u_admin, self.sec_b).status_code, 404)

    def test_roles_without_the_permission_are_refused(self):
        for user in (self.u_p1, self.u_student, self.u_accountant):
            self.assertEqual(self.get(user, self.sec5a).status_code, 403, user.username)

    def test_saving_attendance(self):
        response = self.post_attendance(self.u_t1, self.sec5a, TODAY, {self.asha: "A", self.bimal: "L"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Saved")
        self.assertEqual(AttendanceRecord.objects.get(student=self.asha, date=TODAY).status, "A")
        self.assertEqual(AttendanceRecord.objects.get(student=self.bimal, date=TODAY).marked_by, self.u_t1)
        # Reopening shows what was saved and says it's an update.
        again = self.get(self.u_t1, self.sec5a, TODAY)
        self.assertEqual({r["student"].first_name: r["current"] for r in again.context["rows"]}, {"Asha": "A", "Bimal": "L"})
        self.assertContains(again, "already taken")

    def test_a_missing_choice_saves_nothing(self):
        response = self.post_attendance(self.u_t1, self.sec5a, TODAY, {self.asha: "A"})
        self.assertContains(response, "choose a status for every student")
        self.assertEqual(AttendanceRecord.objects.count(), 0)
        self.assertEqual(
            {r["student"].first_name: r["current"] for r in response.context["rows"]}["Asha"], "A"
        )  # their choice is kept on the page

    def test_an_invalid_status_saves_nothing(self):
        response = self.post_attendance(self.u_t1, self.sec5a, TODAY, {self.asha: "A", self.bimal: "ZZ"})
        self.assertContains(response, "choose a status for every student")
        self.assertEqual(AttendanceRecord.objects.count(), 0)

    def test_forged_extra_students_are_ignored(self):
        response = self.post_attendance(
            self.u_t1, self.sec5a, TODAY, {self.asha: "P", self.bimal: "P", self.chitra: "A", self.zoya: "A"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AttendanceRecord.objects.count(), 2)
        self.assertFalse(AttendanceRecord.objects.filter(student__in=[self.chitra, self.zoya]).exists())

    def test_a_teacher_cannot_post_to_another_class(self):
        self.client.force_login(self.u_t1)
        response = self.client.post(
            reverse("attendance_mark", args=[self.sec5b.pk]), {"date": TODAY.isoformat(), f"status_{self.chitra.pk}": "A"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(AttendanceRecord.objects.count(), 0)

    def test_closed_days_cannot_be_marked(self):
        response = self.post_attendance(self.u_t1, self.sec5a, SUNDAY, {self.asha: "P", self.bimal: "P"})
        self.assertContains(response, "Weekly off")
        self.assertEqual(AttendanceRecord.objects.count(), 0)
        Holiday.objects.create(school=self.school_a, date=TODAY, name="Founders Day")
        response = self.post_attendance(self.u_admin, self.sec5a, TODAY, {self.asha: "P", self.bimal: "P"})
        self.assertContains(response, "Founders Day")
        self.assertEqual(AttendanceRecord.objects.count(), 0)

    def test_closed_day_page_explains_itself(self):
        response = self.get(self.u_t1, self.sec5a, SUNDAY)
        self.assertFalse(response.context["editable"])
        self.assertContains(response, "School is closed")

    def test_future_dates_cannot_be_marked(self):
        tomorrow = TODAY + timedelta(days=1)
        response = self.post_attendance(self.u_admin, self.sec5a, tomorrow, {self.asha: "P", self.bimal: "P"})
        self.assertContains(response, "future date")
        self.assertEqual(AttendanceRecord.objects.count(), 0)

    def test_teacher_is_limited_to_the_edit_window_but_the_office_is_not(self):
        old = TODAY - timedelta(days=15)  # a Tuesday
        response = self.post_attendance(self.u_t1, self.sec5a, old, {self.asha: "P", self.bimal: "P"})
        self.assertContains(response, "up to 7 days back")
        self.assertEqual(AttendanceRecord.objects.count(), 0)
        page = self.get(self.u_t1, self.sec5a, old)
        self.assertFalse(page.context["editable"])
        self.assertNotContains(page, "Save attendance")

        self.post_attendance(self.u_admin, self.sec5a, old, {self.asha: "A", self.bimal: "P"})
        self.assertEqual(AttendanceRecord.objects.get(student=self.asha, date=old).status, "A")

    def test_teacher_can_fix_yesterdays_mistake(self):
        friday = date(2026, 10, 30)
        self.post_attendance(self.u_t1, self.sec5a, friday, {self.asha: "P", self.bimal: "P"})
        self.post_attendance(self.u_t1, self.sec5a, friday, {self.asha: "A", self.bimal: "P"})
        self.assertEqual(AttendanceRecord.objects.filter(date=friday).count(), 2)
        self.assertEqual(AttendanceRecord.objects.get(student=self.asha, date=friday).status, "A")

    def test_garbage_dates_fall_back_to_today(self):
        self.client.force_login(self.u_t1)
        response = self.client.get(reverse("attendance_mark", args=[self.sec5a.pk]), {"date": "not-a-date"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["on_date"], TODAY)

    def test_subject_teacher_can_mark_once_the_school_allows_it(self):
        self.assertEqual(self.get(self.u_t2, self.sec5b).status_code, 404)
        self.set_settings(attendance_marked_by="assigned_teachers")
        self.assertEqual(self.get(self.u_t2, self.sec5b).status_code, 200)

    def test_next_day_link_stops_at_today(self):
        self.assertIsNone(self.get(self.u_t1, self.sec5a).context["next_date"])
        self.assertEqual(self.get(self.u_t1, self.sec5a, TODAY - timedelta(days=1)).context["next_date"], TODAY)


class HomeScreenTests(ClockMixin, WorldTestCase):
    def home(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("attendance_home"))
        self.assertEqual(response.status_code, 200)
        return response

    def rows(self, user):
        return {str(r["section"]): r for r in self.home(user).context["class_rows"]}

    def test_class_teacher_sees_their_class_and_can_mark_it(self):
        rows = self.rows(self.u_t1)
        self.assertEqual(list(rows), ["Class 5-A"])
        self.assertTrue(rows["Class 5-A"]["can_mark"])
        self.assertEqual(rows["Class 5-A"]["state"], "not_marked")

    def test_state_changes_once_attendance_is_taken(self):
        self.post_attendance(self.u_t1, self.sec5a, TODAY, {self.asha: "A", self.bimal: "P"})
        row = self.rows(self.u_t1)["Class 5-A"]
        self.assertEqual((row["state"], row["marked"], row["present"], row["total"]), ("marked", 2, 1, 2))

    def test_subject_teacher_can_look_but_not_mark(self):
        rows = self.rows(self.u_t2)
        self.assertEqual(list(rows), ["Class 5-B"])
        self.assertTrue(rows["Class 5-B"]["can_view"])
        self.assertFalse(rows["Class 5-B"]["can_mark"])

    def test_admin_sees_every_class(self):
        self.assertEqual(set(self.rows(self.u_admin)), {"Class 5-A", "Class 5-B", "Class 6-A"})

    def test_closed_day_is_shown_as_closed(self):
        Holiday.objects.create(school=self.school_a, date=TODAY, name="Founders Day")
        response = self.home(self.u_t1)
        self.assertEqual(response.context["class_rows"][0]["state"], "closed")
        self.assertContains(response, "Founders Day")

    def test_parent_sees_only_their_children_with_percentages(self):
        self.mark(self.sec5a, TODAY - timedelta(days=1), {self.asha: "P", self.bimal: "A"})
        self.mark(self.sec5a, TODAY, {self.asha: "A", self.bimal: "A"})
        response = self.home(self.u_p1)
        self.assertEqual(response.context["class_rows"], [])
        kids = {k.first_name: k for k in response.context["kids"]}
        self.assertEqual(set(kids), {"Asha", "Chitra"})
        self.assertEqual(kids["Asha"].att_month["percentage"], 50.0)
        self.assertIsNone(kids["Chitra"].att_month["percentage"])  # nothing taken for her class yet
        self.assertNotContains(response, "Bimal")

    def test_student_sees_only_themselves(self):
        response = self.home(self.u_student)
        self.assertEqual([k.first_name for k in response.context["kids"]], ["Asha"])

    def test_dual_role_user_gets_both_panels(self):
        response = self.home(self.u_dual)
        self.assertEqual([str(r["section"]) for r in response.context["class_rows"]], ["Class 6-A"])
        self.assertEqual([k.first_name for k in response.context["kids"]], ["Bimal"])

    def test_nav_shows_attendance_to_roles_that_have_it(self):
        for user in (self.u_admin, self.u_t1, self.u_p1, self.u_student):
            self.client.force_login(user)
            self.assertContains(self.client.get(reverse("dashboard")), ">Attendance<", msg_prefix=user.username)
        self.client.force_login(self.u_accountant)  # accountants have no attendance permission
        self.assertNotContains(self.client.get(reverse("dashboard")), ">Attendance<")
        self.assertEqual(self.client.get(reverse("attendance_home")).status_code, 403)


class StudentAttendanceScreenTests(ClockMixin, WorldTestCase):
    def get(self, user, student, **params):
        self.client.force_login(user)
        return self.client.get(reverse("attendance_student", args=[student.pk]), params)

    def test_who_can_open_which_student(self):
        self.assertEqual(self.get(self.u_p1, self.asha).status_code, 200)
        self.assertEqual(self.get(self.u_p1, self.chitra).status_code, 200)  # sibling
        self.assertEqual(self.get(self.u_p1, self.bimal).status_code, 404)
        self.assertEqual(self.get(self.u_student, self.asha).status_code, 200)
        self.assertEqual(self.get(self.u_student, self.bimal).status_code, 404)
        self.assertEqual(self.get(self.u_t1, self.asha).status_code, 200)
        self.assertEqual(self.get(self.u_t1, self.chitra).status_code, 404)
        self.assertEqual(self.get(self.u_admin, self.asha).status_code, 200)
        self.assertEqual(self.get(self.u_admin, self.zoya).status_code, 404)  # other school
        self.assertEqual(self.get(self.u_admin_b, self.asha).status_code, 404)

    def test_summary_and_calendar(self):
        d1, d2, d3 = date(2026, 11, 2), date(2026, 11, 3), TODAY
        self.mark(self.sec5a, d1, {self.asha: "P", self.bimal: "P"})
        self.mark(self.sec5a, d2, {self.asha: "A", self.bimal: "P"})
        self.mark(self.sec5a, d3, {self.asha: "L", self.bimal: "P"})
        response = self.get(self.u_p1, self.asha)
        self.assertEqual(response.context["month"]["percentage"], 66.7)
        self.assertEqual(response.context["year_to_date"]["percentage"], 66.7)
        cells = {c["date"]: c for week in response.context["weeks"] for c in week if c["in_month"]}
        self.assertEqual(cells[d1]["status"], "P")
        self.assertEqual(cells[d2]["status"], "A")
        self.assertTrue(cells[SUNDAY].get("off"))  # weekly off is shown as such
        self.assertTrue(cells[date(2026, 11, 5)].get("future"))
        self.assertEqual([r.date for r in response.context["exceptions"]], [d2, d3])

    def test_unmarked_working_days_in_the_past_are_flagged(self):
        response = self.get(self.u_p1, self.asha)
        cells = {c["date"]: c for week in response.context["weeks"] for c in week if c["in_month"]}
        self.assertTrue(cells[date(2026, 11, 2)].get("unmarked"))

    def test_holiday_appears_on_the_calendar(self):
        Holiday.objects.create(school=self.school_a, date=date(2026, 11, 3), name="Founders Day")
        cells = {c["date"]: c for week in self.get(self.u_p1, self.asha).context["weeks"] for c in week if c["in_month"]}
        self.assertEqual(cells[date(2026, 11, 3)]["note"], "Founders Day")

    def test_month_navigation_is_safe(self):
        self.mark(self.sec5a, date(2026, 10, 15), {self.asha: "A", self.bimal: "P"})
        october = self.get(self.u_p1, self.asha, month="2026-10")
        self.assertEqual(october.context["month"]["counts"]["A"], 1)
        self.assertEqual(october.context["prev_key"], "2026-09")
        self.assertEqual(october.context["next_key"], "2026-11")
        for junk in ("garbage", "2030-01", "1999-01", "2026-99"):
            response = self.get(self.u_p1, self.asha, month=junk)
            self.assertEqual(response.status_code, 200, junk)
            self.assertEqual(response.context["month_start"], date(2026, 11, 1), junk)

    def test_no_data_yet_does_not_show_zero_percent(self):
        response = self.get(self.u_p1, self.asha)
        self.assertIsNone(response.context["month"]["percentage"])
        self.assertContains(response, "No attendance has been recorded yet")


class RegisterScreenTests(ClockMixin, WorldTestCase):
    def get(self, user, section, **params):
        self.client.force_login(user)
        return self.client.get(reverse("attendance_register", args=[section.pk]), params)

    def test_who_can_open_which_register(self):
        self.assertEqual(self.get(self.u_t1, self.sec5a).status_code, 200)
        self.assertEqual(self.get(self.u_t1, self.sec5b).status_code, 404)
        self.assertEqual(self.get(self.u_t2, self.sec5b).status_code, 200)  # subject teachers may view
        self.assertEqual(self.get(self.u_admin, self.sec6a).status_code, 200)
        self.assertEqual(self.get(self.u_admin, self.sec_b).status_code, 404)
        self.assertEqual(self.get(self.u_admin_b, self.sec5a).status_code, 404)
        self.assertEqual(self.get(self.u_p1, self.sec5a).status_code, 404)  # parents don't get class registers
        self.assertEqual(self.get(self.u_student, self.sec5a).status_code, 404)

    def test_register_contents(self):
        self.mark(self.sec5a, date(2026, 11, 2), {self.asha: "P", self.bimal: "A"})
        self.mark(self.sec5a, date(2026, 11, 3), {self.asha: "P", self.bimal: "P"})
        response = self.get(self.u_t1, self.sec5a)
        rows = {r["student"].first_name: r for r in response.context["rows"]}
        self.assertEqual(rows["Asha"]["summary"]["percentage"], 100.0)
        self.assertEqual(rows["Bimal"]["summary"]["percentage"], 50.0)
        days = {d["date"]: d for d in response.context["days"]}
        self.assertEqual(len(days), 30)  # November
        self.assertEqual(days[date(2026, 11, 2)]["present"], 1)
        self.assertEqual(days[date(2026, 11, 3)]["present"], 2)
        self.assertEqual(days[SUNDAY]["off"], "Weekly off")
        self.assertEqual(len(rows["Asha"]["cells"]), 30)
        self.assertEqual(rows["Asha"]["cells"][1], "P")  # 2 Nov is the second column

    def test_register_offers_marking_only_to_those_who_may(self):
        self.assertTrue(self.get(self.u_t1, self.sec5a).context["can_mark"])
        self.assertFalse(self.get(self.u_t2, self.sec5b).context["can_mark"])

    def test_query_count_does_not_grow_with_class_size(self):
        self.client.force_login(self.u_t1)
        url = reverse("attendance_register", args=[self.sec5a.pk])
        self.client.get(url)  # warm-up (the first request also saves the session)

        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(ctx)

        before = queries()
        for i in range(20):
            s = Student.objects.create(school=self.school_a, admission_no=f"R{i}", first_name=f"Extra{i}")
            Enrollment.objects.create(school=self.school_a, student=s, section=self.sec5a, roll_no=10 + i)
        self.assertEqual(queries(), before)


class DashboardIntegrationTests(ClockMixin, WorldTestCase):
    def dash(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("dashboard"))

    def test_admin_sees_todays_totals(self):
        self.assertContains(self.dash(self.u_admin), "No class has taken attendance yet today")
        self.mark(self.sec5a, TODAY, {self.asha: "P", self.bimal: "A"})
        today = self.dash(self.u_admin).context["attendance_today"]
        self.assertEqual((today["present"], today["marked"], today["sections_marked"]), (1, 2, 1))

    def test_accountant_does_not_see_attendance(self):
        self.assertNotIn("attendance_today", self.dash(self.u_accountant).context)

    def test_teacher_card_prompts_for_attendance_then_confirms(self):
        item = self.dash(self.u_t1).context["my_sections"][0]
        self.assertEqual((item["can_mark"], item["att_state"]), (True, "not_marked"))
        self.mark(self.sec5a, TODAY, {self.asha: "P", self.bimal: "P"})
        self.assertEqual(self.dash(self.u_t1).context["my_sections"][0]["att_state"], "marked")

    def test_subject_teacher_card_has_no_marking_prompt(self):
        item = self.dash(self.u_t2).context["my_sections"][0]
        self.assertFalse(item["can_mark"])
        self.assertNotIn("att_state", item)

    def test_parent_card_shows_this_months_percentage(self):
        self.mark(self.sec5a, TODAY, {self.asha: "P", self.bimal: "A"})
        response = self.dash(self.u_p1)
        children = {c.first_name: c for c in response.context["children"]}
        self.assertEqual(children["Asha"].att_month["percentage"], 100.0)
        self.assertContains(response, "Attendance this month")

    def test_closed_day_shows_on_teacher_card(self):
        self.client.force_login(self.u_t1)
        Holiday.objects.create(school=self.school_a, date=TODAY, name="Founders Day")
        self.assertEqual(self.client.get(reverse("dashboard")).context["my_sections"][0]["att_state"], "closed")


class AdminSiteTests(ClockMixin, WorldTestCase):
    def test_office_can_view_attendance_records_of_their_school_only(self):
        self.mark(self.sec5a, TODAY, {self.asha: "A", self.bimal: "P"})
        self.mark(self.sec_b, TODAY, {self.zoya: "A"}, user=self.u_admin_b)
        self.client.force_login(self.u_office)
        response = self.client.get(reverse("admin:attendance_attendancerecord_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asha")
        self.assertNotContains(response, "Zoya")

    def test_only_the_principal_edits_holidays(self):
        self.client.force_login(self.u_office)
        self.assertEqual(self.client.get(reverse("admin:attendance_holiday_add")).status_code, 403)
        self.client.force_login(self.u_admin)
        response = self.client.post(
            reverse("admin:attendance_holiday_add"), {"date": "2026-12-25", "name": "Christmas", "school": self.school_b.pk}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Holiday.objects.get(name="Christmas").school, self.school_a)  # school can't be forged
