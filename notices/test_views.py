from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from academics.models import TeachingAssignment
from accounts.models import User
from fees import services as fee_services
from people.models import Guardian, Student, StudentGuardian

from . import reminders, services
from .models import Notice, NoticeRead, Reminder
from .test_reminders import OCT, TODAY, ReminderCase


class ScreenCase(ReminderCase):
    def get(self, user, name, *args, **params):
        self.client.force_login(user)
        return self.client.get(reverse(name, args=args), params)

    def post(self, user, name, *args, **data):
        self.client.force_login(user)
        return self.client.post(reverse(name, args=args), data)

    def notice(self, author=None, **kw):
        kw.setdefault("title", "Annual Day")
        kw.setdefault("body", "Join us on Friday.")
        kw.setdefault("everyone", True) if not {"role_ids", "grade_ids", "section_ids"} & set(kw) else None
        return services.create_notice(self.school_a, author or self.u_office, **kw)


class ListScreenTests(ScreenCase):
    def test_signing_in_is_required_and_a_role_in_the_school_too(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("notice_list")).status_code, 302)
        self.assertEqual(self.get(self.u_orphan, "notice_list").status_code, 403)

    def test_everyone_with_a_role_sees_what_is_meant_for_them(self):
        self.notice(title="For all")
        self.notice(title="Class 6 only", grade_ids=[self.grade6.pk])
        self.notice(title="5B only", section_ids=[self.sec5b.pk])
        expectations = {
            self.u_p1: {"For all", "5B only"}, self.u_p2: {"For all"}, self.u_student: {"For all"},
            self.u_t2: {"For all", "5B only"}, self.u_dual: {"For all", "Class 6 only"},
            self.u_office: {"For all", "Class 6 only", "5B only"},
        }
        for user, titles in expectations.items():
            page = self.get(user, "notice_list")
            self.assertEqual(page.status_code, 200)
            for title in ("For all", "Class 6 only", "5B only"):
                if title in titles:
                    self.assertContains(page, title, msg_prefix=user.username)
                else:
                    self.assertNotContains(page, title, msg_prefix=user.username)

    def test_new_notices_are_marked_until_opened(self):
        notice = self.notice()
        self.assertContains(self.get(self.u_p1, "notice_list"), "New</span>")
        self.get(self.u_p1, "notice_detail", notice.pk)
        self.assertNotContains(self.get(self.u_p1, "notice_list"), "new-tag")

    def test_the_notice_card_shows_who_wrote_it_and_who_it_is_for(self):
        self.notice(grade_ids=[self.grade5.pk], role_ids=[self.roles_a["teacher"].pk])
        page = self.get(self.u_p1, "notice_list")
        self.assertContains(page, "From office_a")
        self.assertContains(page, "To Teacher, Class 5")

    def test_filtering_by_kind(self):
        self.notice(title="Holiday on Monday", category="holiday")
        self.notice(title="Sports day", category="event")
        page = self.get(self.u_p1, "notice_list", category="holiday")
        self.assertContains(page, "Holiday on Monday")
        self.assertNotContains(page, "Sports day")
        self.assertContains(self.get(self.u_p1, "notice_list", category="nonsense"), "Sports day")  # unknown kinds are ignored

    def test_an_empty_list_says_so(self):
        self.assertContains(self.get(self.u_p1, "notice_list"), "No notices right now.")

    def test_long_lists_are_paged(self):
        for n in range(25):
            self.notice(title=f"Notice number {n:02d}")
        first = self.get(self.u_p1, "notice_list")
        self.assertContains(first, "Page 1 of 2")
        self.assertEqual(first.content.decode().count('class="card notice-item'), 20)
        second = self.get(self.u_p1, "notice_list", page=2)
        self.assertEqual(second.content.decode().count('class="card notice-item'), 5)

    def test_the_page_costs_a_fixed_number_of_queries(self):
        self.notice(grade_ids=[self.grade5.pk])
        self.client.force_login(self.u_p1)
        self.client.get(reverse("notice_list"))  # warm-up
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("notice_list"))
        for n in range(15):
            self.notice(title=f"More {n}", section_ids=[self.sec5a.pk])
        with CaptureQueriesContext(connection) as big:
            self.client.get(reverse("notice_list"))
        self.assertLessEqual(len(big), len(small) + 2)

    def test_who_gets_the_buttons(self):
        post = reverse("notice_new")
        for user in (self.u_office, self.u_admin, self.u_t1):
            self.assertContains(self.get(user, "notice_list"), post, msg_prefix=user.username)
        for user in (self.u_p1, self.u_student, self.u_accountant, self.u_t3):
            self.assertNotContains(self.get(user, "notice_list"), post, msg_prefix=user.username)
        remind = reverse("fee_reminders")
        for user in (self.u_office, self.u_accountant, self.u_admin):
            self.assertContains(self.get(user, "notice_list"), remind, msg_prefix=user.username)
        for user in (self.u_t1, self.u_p1):
            self.assertNotContains(self.get(user, "notice_list"), remind, msg_prefix=user.username)

    def test_posted_by_me_and_everything_posted(self):
        mine = self.notice(self.u_t1, title="By Tara", section_ids=[self.sec5a.pk])
        self.notice(self.u_office, title="By office")
        services.withdraw_notice(mine, self.u_t1)
        page = self.get(self.u_t1, "notice_list", view="mine")
        self.assertContains(page, "By Tara")
        self.assertContains(page, "Withdrawn")
        self.assertNotContains(page, "By office")
        self.assertNotContains(self.get(self.u_t1, "notice_list"), "By Tara")  # gone from their own inbox
        everything = self.get(self.u_office, "notice_list", view="all")
        self.assertContains(everything, "By Tara")
        self.assertContains(everything, "By office")

    def test_only_school_wide_posters_get_the_everything_view(self):
        self.notice(self.u_office, title="Secret plans", section_ids=[self.sec6a.pk])
        self.assertNotContains(self.get(self.u_t1, "notice_list"), 'view=all')
        self.assertNotContains(self.get(self.u_t1, "notice_list", view="all"), "Secret plans")  # falls back to their own inbox
        self.assertNotContains(self.get(self.u_p1, "notice_list", view="mine"), "Secret plans")
        self.assertContains(self.get(self.u_office, "notice_list"), 'view=all')

    def test_expired_notices_are_hidden_but_the_author_still_sees_them_marked(self):
        self.notice(title="Short lived", expires_on=TODAY)
        self.assertContains(self.get(self.u_p1, "notice_list"), "Short lived")
        from unittest.mock import patch
        with patch("notices.services.today", return_value=TODAY + timedelta(days=1)):
            self.assertNotContains(self.get(self.u_p1, "notice_list"), "Short lived")
            page = self.get(self.u_office, "notice_list", view="mine")
            self.assertContains(page, "Short lived")
            self.assertContains(page, "Ended")

    def test_reminders_for_you_are_listed_first(self):
        self.client.force_login(self.u_p1)
        reminders.create_reminder(self.school_a, self.u_p1, "fee", "Fee reminder for Asha", "Rs 1,000 is overdue.", key="a",
                                  link=reverse("fee_student", args=[self.asha.pk]))
        page = self.client.get(reverse("notice_list"))
        self.assertContains(page, "Reminders for you")
        self.assertContains(page, "Fee reminder for Asha")
        self.assertContains(page, "Mark all as read")
        self.assertNotContains(self.get(self.u_p2, "notice_list"), "Reminders for you")  # not theirs

    def test_opened_reminders_stay_in_view_a_little_longer(self):
        for n in range(8):
            reminders.create_reminder(self.school_a, self.u_p1, "fee", f"Old {n}", "x", key=f"k{n}")
        reminders.mark_all_read(self.u_p1, self.school_a)
        page = self.get(self.u_p1, "notice_list")
        self.assertEqual(page.content.decode().count("Old "), 5)
        self.assertNotContains(page, "Mark all as read")


class DetailScreenTests(ScreenCase):
    def test_opening_a_notice_shows_it_and_marks_it_read(self):
        notice = self.notice(body="Line one\n\nLine two with https://example.com/page")
        page = self.get(self.u_p1, "notice_detail", notice.pk)
        self.assertContains(page, "Annual Day")
        self.assertContains(page, "<p>Line one</p>", html=True)
        self.assertContains(page, 'href="https://example.com/page"')
        self.assertContains(page, "From office_a")
        self.assertContains(page, "For: Everyone")
        self.assertTrue(NoticeRead.objects.filter(notice=notice, user=self.u_p1).exists())

    def test_text_is_shown_as_text_never_as_markup(self):
        notice = self.notice(title="<b>Bold title</b>", body='<script>alert("x")</script> <img src=x onerror=alert(1)>')
        page = self.get(self.u_p1, "notice_detail", notice.pk)
        body = page.content.decode()
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img src=x", body)
        self.assertNotIn("<b>Bold title</b>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&lt;b&gt;Bold title&lt;/b&gt;", body)

    def test_a_notice_that_is_not_for_you_does_not_exist(self):
        notice = self.notice(section_ids=[self.sec5b.pk])
        self.assertEqual(self.get(self.u_p2, "notice_detail", notice.pk).status_code, 404)
        self.assertEqual(self.get(self.u_p1, "notice_detail", notice.pk).status_code, 200)
        self.assertFalse(NoticeRead.objects.filter(notice=notice, user=self.u_p2).exists())

    def test_another_schools_notice_does_not_exist(self):
        other = services.create_notice(self.school_b, self.u_admin_b, title="Beta", body="x", everyone=True)
        for user in (self.u_admin, self.u_p1):
            self.assertEqual(self.get(user, "notice_detail", other.pk).status_code, 404)
        self.assertEqual(self.get(self.u_admin, "notice_detail", 999999).status_code, 404)

    def test_a_withdrawn_notice_is_gone_for_readers_but_not_for_those_who_manage_it(self):
        notice = self.notice(self.u_t1, section_ids=[self.sec5a.pk])
        services.withdraw_notice(notice, self.u_t1)
        self.assertEqual(self.get(self.u_p1, "notice_detail", notice.pk).status_code, 404)
        for user in (self.u_t1, self.u_office):
            page = self.get(user, "notice_detail", notice.pk)
            self.assertContains(page, "Withdrawn.")
            self.assertNotContains(page, reverse("notice_edit", args=[notice.pk]))
        self.assertEqual(self.get(self.u_t2, "notice_detail", notice.pk).status_code, 404)

    def test_an_expired_notice_is_gone_for_readers(self):
        notice = self.notice(expires_on=TODAY)
        from unittest.mock import patch
        with patch("notices.services.today", return_value=TODAY + timedelta(days=1)):
            self.assertEqual(self.get(self.u_p1, "notice_detail", notice.pk).status_code, 404)
            page = self.get(self.u_office, "notice_detail", notice.pk)
            self.assertContains(page, "Ended on 4 Nov 2026")
            self.assertFalse(NoticeRead.objects.filter(notice=notice, user=self.u_office).exists())  # not marked read once over

    def test_the_author_sees_how_far_it_reached_and_the_buttons(self):
        notice = self.notice(self.u_t1, grade_ids=None or [], section_ids=[self.sec5a.pk])
        services.mark_read(notice, self.u_p1)
        page = self.get(self.u_t1, "notice_detail", notice.pk)
        self.assertContains(page, "How far it reached")
        self.assertContains(page, "of 4 people have read it")  # Asha's login, p1, p2, dual (the author, the class teacher, is not counted)
        self.assertContains(page, reverse("notice_edit", args=[notice.pk]))
        self.assertContains(page, reverse("notice_withdraw", args=[notice.pk]))

    def test_readers_see_neither_the_reach_nor_the_buttons(self):
        notice = self.notice()
        page = self.get(self.u_p1, "notice_detail", notice.pk)
        self.assertNotContains(page, "How far it reached")
        self.assertNotContains(page, reverse("notice_edit", args=[notice.pk]))
        self.assertNotContains(page, reverse("notice_withdraw", args=[notice.pk]))

    def test_a_teacher_cannot_manage_the_offices_notice(self):
        notice = self.notice(self.u_office, section_ids=[self.sec5a.pk])
        page = self.get(self.u_t1, "notice_detail", notice.pk)
        self.assertNotContains(page, "How far it reached")
        self.assertNotContains(page, reverse("notice_edit", args=[notice.pk]))

    def test_students_nobody_can_reach_are_pointed_out_to_the_office_only(self):
        notice = self.notice(self.u_t3 if False else self.u_office, grade_ids=[self.grade6.pk])  # Dev has no parent or student login
        office = self.get(self.u_office, "notice_detail", notice.pk)
        self.assertContains(office, "1 student</strong> in this audience has no parent or student login yet")
        self.assertContains(office, reverse("setup_home"))
        teachers = self.notice(self.u_dual, section_ids=[self.sec6a.pk])
        page = self.get(self.u_dual, "notice_detail", teachers.pk)
        self.assertContains(page, "no parent or student login yet")
        self.assertNotContains(page, reverse("setup_home"))  # a teacher can't create logins


class ComposeScreenTests(ScreenCase):
    def valid(self, **extra):
        return {"title": "Parents meeting", "body": "On Saturday at 10.", "category": "event", **extra}

    def test_the_office_may_choose_anyone(self):
        page = self.get(self.u_office, "notice_new")
        self.assertContains(page, "Everyone in the school")
        self.assertContains(page, "By role")
        self.assertContains(page, "Whole classes")
        for label in ("Class 5-A", "Class 5-B", "Class 6-A", "Parent / Guardian", "Class 6"):
            self.assertContains(page, label)

    def test_a_teacher_sees_only_their_own_sections(self):
        page = self.get(self.u_t1, "notice_new")
        self.assertContains(page, "Your classes")
        self.assertContains(page, "Class 5-A")
        for absent in ("Class 5-B", "Class 6-A", "Everyone in the school", "By role", "Whole classes"):
            self.assertNotContains(page, absent)

    def test_a_teacher_with_no_classes_gets_an_explanation(self):
        page = self.get(self.u_t3, "notice_new")
        self.assertEqual(page.status_code, 403)
        self.assertContains(page, "you aren't assigned to any class yet", status_code=403)

    def test_people_who_cannot_post_are_turned_away(self):
        for user in (self.u_p1, self.u_student, self.u_accountant, self.u_orphan):
            self.assertEqual(self.get(user, "notice_new").status_code, 403, user.username)
            self.assertEqual(self.post(user, "notice_new", **self.valid(everyone="on")).status_code, 403, user.username)
        self.assertEqual(Notice.objects.count(), 0)

    def test_posting_to_the_whole_school(self):
        response = self.post(self.u_office, "notice_new", **self.valid(everyone="on"))
        notice = Notice.objects.get()
        self.assertRedirects(response, reverse("notice_detail", args=[notice.pk]), fetch_redirect_response=False)
        self.assertEqual((notice.title, notice.category, notice.author), ("Parents meeting", "event", self.u_office))
        self.assertEqual(notice.audience_labels(), ["Everyone"])
        self.assertContains(self.client.get(response["Location"]), "Your notice has been posted.")

    def test_posting_to_roles_classes_and_sections(self):
        self.post(
            self.u_admin, "notice_new",
            **self.valid(roles=[self.roles_a["teacher"].pk], grades=[self.grade6.pk], sections=[self.sec5a.pk, self.sec5b.pk],
                         expires_on="2026-12-01"),
        )
        notice = Notice.objects.get()
        self.assertEqual(notice.audience_labels(), ["Teacher", "Class 6", "Class 5-A", "Class 5-B"])
        self.assertEqual(str(notice.expires_on), "2026-12-01")

    def test_a_teacher_posts_to_their_section(self):
        self.post(self.u_t1, "notice_new", **self.valid(sections=[self.sec5a.pk]))
        self.assertEqual(Notice.objects.get().audience_labels(), ["Class 5-A"])

    def test_a_teacher_cannot_slip_in_an_audience_they_were_not_offered(self):
        for forged in (
            {"sections": [self.sec5b.pk]},
            {"sections": [self.sec5a.pk, self.sec6a.pk]},
            {"everyone": "on"},
            {"grades": [self.grade5.pk]},
            {"roles": [self.roles_a["parent"].pk]},
        ):
            response = self.post(self.u_t1, "notice_new", **self.valid(**forged))
            self.assertEqual(response.status_code, 200, forged)  # shown again with an error
        self.assertEqual(Notice.objects.count(), 0)

    def test_a_school_cannot_address_another_schools_classes(self):
        for forged in ({"sections": [self.sec_b.pk]}, {"grades": [self.grade5_b.pk]}, {"roles": [self.roles_b["parent"].pk]}):
            self.assertEqual(self.post(self.u_office, "notice_new", **self.valid(**forged)).status_code, 200)
        self.assertEqual(Notice.objects.count(), 0)

    def test_mistakes_are_explained_and_nothing_is_saved(self):
        for data, message in (
            (self.valid(), "Please choose who the notice is for."),
            (self.valid(everyone="on", title=""), "This field is required."),
            (self.valid(everyone="on", body=""), "This field is required."),
            (self.valid(everyone="on", category="gossip"), "Select a valid choice."),
            (self.valid(everyone="on", expires_on="2026-11-03"), "can&#x27;t be in the past"),
            (self.valid(everyone="on", expires_on="not a date"), "Enter a valid date."),
            (self.valid(everyone="on", title="x" * 121), "at most 120 characters"),
        ):
            response = self.post(self.u_office, "notice_new", **data)
            self.assertContains(response, message, msg_prefix=str(data))
        self.assertEqual(Notice.objects.count(), 0)

    def test_what_was_typed_is_kept_when_there_is_a_mistake(self):
        response = self.post(self.u_office, "notice_new", **self.valid(title="Kept title"))
        self.assertContains(response, "Kept title")
        self.assertContains(response, "On Saturday at 10.")


class EditAndWithdrawScreenTests(ScreenCase):
    def test_the_author_can_fix_the_wording(self):
        notice = self.notice(self.u_t1, section_ids=[self.sec5a.pk])
        page = self.get(self.u_t1, "notice_edit", notice.pk)
        self.assertContains(page, "Annual Day")
        self.assertContains(page, "Class 5-A")
        self.assertNotContains(page, "Who is it for?")  # the audience can't be changed
        response = self.post(self.u_t1, "notice_edit", notice.pk, title="Annual Day (new time)", body="Now at 5pm.", category="event")
        self.assertRedirects(response, reverse("notice_detail", args=[notice.pk]))
        notice.refresh_from_db()
        self.assertEqual((notice.title, notice.body, notice.category), ("Annual Day (new time)", "Now at 5pm.", "event"))
        self.assertIsNotNone(notice.edited_at)
        self.assertContains(self.get(self.u_p1, "notice_detail", notice.pk), "edited 4 Nov 2026".replace("4 Nov 2026", timezone.localdate().strftime("%-d %b %Y")))

    def test_the_office_may_edit_a_teachers_notice_but_a_teacher_not_anothers(self):
        notice = self.notice(self.u_t1, section_ids=[self.sec5a.pk])
        self.assertEqual(self.get(self.u_office, "notice_edit", notice.pk).status_code, 200)
        self.assertEqual(self.get(self.u_t2, "notice_edit", notice.pk).status_code, 404)
        office_notice = self.notice(self.u_office)
        self.assertEqual(self.get(self.u_t1, "notice_edit", office_notice.pk).status_code, 404)

    def test_readers_cannot_reach_the_edit_page(self):
        notice = self.notice()
        for user in (self.u_p1, self.u_student, self.u_accountant):
            self.assertEqual(self.get(user, "notice_edit", notice.pk).status_code, 403, user.username)
        # The principal of another school works inside their own school, where this notice does not exist.
        self.assertEqual(self.get(self.u_admin_b, "notice_edit", notice.pk).status_code, 404)

    def test_edit_mistakes_are_explained(self):
        notice = self.notice()
        response = self.post(self.u_office, "notice_edit", notice.pk, title="", body="x", category="general")
        self.assertContains(response, "This field is required.")
        notice.refresh_from_db()
        self.assertEqual(notice.title, "Annual Day")

    def test_a_withdrawn_notice_cannot_be_edited(self):
        notice = self.notice()
        services.withdraw_notice(notice, self.u_office)
        response = self.get(self.u_office, "notice_edit", notice.pk)
        self.assertRedirects(response, reverse("notice_detail", args=[notice.pk]))

    def test_withdrawing(self):
        notice = self.notice(self.u_t1, section_ids=[self.sec5a.pk])
        self.assertEqual(self.client.force_login(self.u_t1) or self.client.get(reverse("notice_withdraw", args=[notice.pk])).status_code, 405)
        response = self.post(self.u_t1, "notice_withdraw", notice.pk)
        self.assertRedirects(response, reverse("notice_list"))
        notice.refresh_from_db()
        self.assertTrue(notice.is_withdrawn)
        self.assertEqual(self.get(self.u_p1, "notice_detail", notice.pk).status_code, 404)

    def test_the_wrong_people_cannot_withdraw(self):
        notice = self.notice(self.u_office)
        self.assertEqual(self.post(self.u_t1, "notice_withdraw", notice.pk).status_code, 404)   # a poster, but not theirs
        for user in (self.u_p1, self.u_accountant):
            self.assertEqual(self.post(user, "notice_withdraw", notice.pk).status_code, 403, user.username)
        self.assertEqual(self.post(self.u_admin_b, "notice_withdraw", notice.pk).status_code, 404)  # not in their school
        notice.refresh_from_db()
        self.assertFalse(notice.is_withdrawn)


class ReminderScreenTests(ScreenCase):
    def setUp(self):
        super().setUp()
        self.reminder, _ = reminders.create_reminder(
            self.school_a, self.u_p1, "fee", "Fee reminder for Asha", "Overdue.", key="k",
            link=reverse("fee_student", args=[self.asha.pk]),
        )

    def test_opening_a_reminder_marks_it_read_and_goes_to_its_page(self):
        response = self.get(self.u_p1, "reminder_open", self.reminder.pk)
        self.assertRedirects(response, reverse("fee_student", args=[self.asha.pk]), fetch_redirect_response=False)
        self.reminder.refresh_from_db()
        self.assertTrue(self.reminder.is_read)

    def test_a_reminder_belongs_to_one_person(self):
        for user in (self.u_p2, self.u_office, self.u_admin):
            self.assertEqual(self.get(user, "reminder_open", self.reminder.pk).status_code, 404, user.username)
        self.reminder.refresh_from_db()
        self.assertFalse(self.reminder.is_read)

    def test_a_link_to_anywhere_else_is_never_followed(self):
        for bad in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
            Reminder.objects.filter(pk=self.reminder.pk).update(link=bad)
            response = self.get(self.u_p1, "reminder_open", self.reminder.pk)
            self.assertRedirects(response, reverse("notice_list"), fetch_redirect_response=False, msg_prefix=bad)

    def test_mark_all_read(self):
        reminders.create_reminder(self.school_a, self.u_p1, "fee", "Second", "x", key="k2")
        self.assertEqual(self.client.force_login(self.u_p1) or self.client.get(reverse("reminders_read_all")).status_code, 405)
        response = self.post(self.u_p1, "reminders_read_all")
        self.assertRedirects(response, reverse("notice_list"))
        self.assertEqual(reminders.unread_reminders(self.u_p1, self.school_a).count(), 0)

    def test_people_outside_the_school_have_no_reminders(self):
        self.assertEqual(self.get(self.u_pb, "reminder_open", self.reminder.pk).status_code, 404)  # a school-B parent: not theirs
        self.assertEqual(self.get(self.u_orphan, "reminder_open", self.reminder.pk).status_code, 403)


class FeeReminderScreenTests(ScreenCase):
    def setUp(self):
        super().setUp()
        self.bill_october()

    def test_the_list_shows_who_will_be_told_and_who_must_be_phoned(self):
        nila = Guardian.objects.create(school=self.school_a, first_name="Nila", last_name="Rao", phone="9000000011")
        StudentGuardian.objects.create(school=self.school_a, student=self.dev, guardian=nila)
        page = self.get(self.u_accountant, "fee_reminders")
        self.assertContains(page, "Asha Test")
        self.assertContains(page, "3 ready to remind")
        self.assertContains(page, "1 with no parent login")
        self.assertContains(page, "No parent login")
        self.assertContains(page, "Nila Rao: <span class=\"nowrap\">9000000011</span>", html=False)
        self.assertEqual(page.content.decode().count('type="checkbox" name="student"'), 3)  # Dev can't be ticked
        self.assertContains(page, "Send reminders to the ticked families")

    def test_who_may_open_the_screen(self):
        for user in (self.u_accountant, self.u_office, self.u_admin):
            self.assertEqual(self.get(user, "fee_reminders").status_code, 200, user.username)
        for user in (self.u_t1, self.u_p1, self.u_student, self.u_orphan):
            self.assertEqual(self.get(user, "fee_reminders").status_code, 403, user.username)

    def test_another_schools_principal_sees_only_their_own_schools_dues(self):
        page = self.get(self.u_admin_b, "fee_reminders")
        self.assertContains(page, "No overdue fees")
        self.assertNotContains(page, "Asha Test")

    def test_the_list_can_be_narrowed_to_one_class(self):
        page = self.get(self.u_accountant, "fee_reminders", grade=self.grade6.pk)
        self.assertContains(page, "Dev Test")
        self.assertNotContains(page, "Asha Test")

    def test_sending_reaches_the_parents(self):
        response = self.post(self.u_accountant, "fee_reminders", student=[self.asha.pk, self.bimal.pk])
        self.assertRedirects(response, reverse("fee_reminders"), fetch_redirect_response=False)
        follow = self.client.get(response["Location"])
        self.assertContains(follow, "Sent 3 reminders to 2 families.")
        self.assertEqual(Reminder.objects.count(), 3)
        page = self.get(self.u_p1, "notice_list")
        self.assertContains(page, "Fee reminder for Asha")
        self.assertContains(page, "overdue")
        self.assertEqual(self.get(self.u_p1, "notice_list").content.decode().count("Fee reminder for"), 1)  # Asha, not Chitra

    def test_sending_again_is_explained_not_repeated(self):
        self.post(self.u_accountant, "fee_reminders", student=[self.asha.pk])
        response = self.client.post(reverse("fee_reminders"), {"student": [self.asha.pk]}, follow=True)
        self.assertContains(response, "No reminders were sent.")
        self.assertContains(response, "1 family was reminded in the last week, so they were skipped.")
        self.assertEqual(Reminder.objects.count(), 1)

    def test_families_without_a_login_are_named_so_they_can_be_phoned(self):
        response = self.client.post(reverse("fee_reminders"), {"student": [self.dev.pk]}, follow=True) if self.client.force_login(self.u_accountant) is None else None
        self.assertContains(response, "No parent login to send to for: Dev Test. Please phone these families.")

    def test_a_family_that_has_paid_is_left_out(self):
        self.pay_in_full(self.asha)
        self.client.force_login(self.u_accountant)
        response = self.client.post(reverse("fee_reminders"), {"student": [self.asha.pk, self.bimal.pk]}, follow=True)
        self.assertContains(response, "1 family has no overdue fees any more and was left out.")
        self.assertEqual(Reminder.objects.filter(student=self.asha).count(), 0)

    def test_nothing_ticked_or_nonsense_is_refused_kindly(self):
        self.client.force_login(self.u_accountant)
        empty = self.client.post(reverse("fee_reminders"), {}, follow=True)
        self.assertContains(empty, "Please choose at least one family to remind.")
        junk = self.client.post(reverse("fee_reminders"), {"student": ["abc"]}, follow=True)
        self.assertContains(junk, "Please choose families from the list.")
        self.assertEqual(Reminder.objects.count(), 0)

    def test_people_without_permission_cannot_send(self):
        for user in (self.u_t1, self.u_p1):
            self.assertEqual(self.post(user, "fee_reminders", student=[self.asha.pk]).status_code, 403, user.username)
        self.client.force_login(self.u_admin_b)  # allowed to send, but only for their own school: Asha is not theirs
        response = self.client.post(reverse("fee_reminders"), {"student": [self.asha.pk]}, follow=True)
        self.assertContains(response, "No reminders were sent.")
        self.assertEqual(Reminder.objects.count(), 0)

    def test_the_class_filter_survives_sending(self):
        response = self.post(self.u_accountant, "fee_reminders", student=[self.dev.pk], grade=self.grade6.pk)
        self.assertRedirects(response, f"{reverse('fee_reminders')}?grade={self.grade6.pk}")

    def test_the_dues_page_links_here_for_those_who_may_send(self):
        self.assertContains(self.get(self.u_accountant, "fee_dues"), reverse("fee_reminders"))
        self.assertContains(self.get(self.u_accountant, "fee_dues", grade=self.grade6.pk), f"{reverse('fee_reminders')}?grade={self.grade6.pk}")

    def test_a_clean_slate_says_so(self):
        for student in (self.asha, self.bimal, self.chitra, self.dev):
            self.pay_in_full(student)
        self.assertContains(self.get(self.u_accountant, "fee_reminders"), "No overdue fees")


class NavigationAndDashboardTests(ScreenCase):
    def test_the_notices_tab_shows_how_many_are_new(self):
        self.notice(title="One")
        self.notice(title="Two")
        reminders.create_reminder(self.school_a, self.u_p1, "fee", "R", "x", key="r")
        page = self.get(self.u_p1, "dashboard")
        self.assertContains(page, 'class="nav-count" aria-hidden="true">3</span>', count=2)  # sidebar and phone tab bar
        self.assertContains(page, "(3 new)")
        for n in services.visible_notices(self.u_p1, self.school_a):
            services.mark_read(n, self.u_p1)
        reminders.mark_all_read(self.u_p1, self.school_a)
        self.assertNotContains(self.get(self.u_p1, "dashboard"), "nav-count")

    def test_everyone_with_the_permission_gets_the_tab_and_nobody_else(self):
        for user in (self.u_p1, self.u_student, self.u_t1, self.u_accountant, self.u_office):
            self.assertContains(self.get(user, "dashboard"), reverse("notice_list"), msg_prefix=user.username)
        role = self.roles_a["parent"]
        role.permissions = [p for p in role.permissions if p != "notices.view"]
        role.save()
        self.assertNotContains(self.get(User.objects.get(pk=self.u_p2.pk), "dashboard"), reverse("notice_list"))

    def test_the_badge_counts_only_what_the_person_may_read(self):
        self.notice(section_ids=[self.sec5b.pk])
        self.assertContains(self.get(self.u_p1, "dashboard"), 'aria-hidden="true">1</span>')
        self.assertNotContains(self.get(self.u_p2, "dashboard"), "nav-count")

    def test_the_dashboard_puts_what_is_new_up_front(self):
        self.notice(title="Winter break")
        reminders.create_reminder(self.school_a, self.u_p1, "fee", "Fee reminder for Asha", "Overdue.", key="r",
                                  link=reverse("fee_student", args=[self.asha.pk]))
        page = self.get(self.u_p1, "dashboard")
        self.assertContains(page, "For you")
        self.assertContains(page, "Fee reminder for Asha")
        self.assertContains(page, "Winter break")
        self.assertContains(page, reverse("reminder_open", args=[Reminder.objects.get().pk]))

    def test_the_dashboard_lists_the_latest_notices_for_staff(self):
        for n in range(5):
            self.notice(title=f"News {n}")
        page = self.get(self.u_admin, "dashboard")
        self.assertContains(page, "Latest notices")
        self.assertContains(page, "News 4")
        self.assertContains(page, "News 2")
        self.assertNotContains(page, "News 1")  # only the latest three

    def test_a_quiet_school_shows_nothing_extra(self):
        page = self.get(self.u_p1, "dashboard")
        self.assertNotContains(page, "Latest notices")
        self.assertNotContains(page, "For you")

    def test_the_notices_tab_is_highlighted_on_its_pages(self):
        notice = self.notice()
        for name, args in (("notice_list", []), ("notice_detail", [notice.pk])):
            page = self.get(self.u_p1, name, *args)
            self.assertContains(page, f'href="{reverse("notice_list")}" class="active" aria-current="page"', count=2)

    def test_the_admin_site_shows_notices_read_only(self):
        self.notice()
        self.client.force_login(self.u_office)
        self.assertEqual(self.client.get(reverse("admin:notices_notice_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:notices_notice_add")).status_code, 403)
        notice = Notice.objects.get()
        self.assertEqual(self.client.post(reverse("admin:notices_notice_delete", args=[notice.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(Notice.objects.count(), 1)
