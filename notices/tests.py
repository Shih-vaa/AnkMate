from datetime import date, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from academics.models import Enrollment
from accounts.models import Membership, User
from core.testing import WorldTestCase
from people.models import Student

from . import services
from .models import Notice, NoticeRead, NoticeTarget
from .services import NoticeError

TODAY = date(2026, 11, 4)  # a Wednesday, inside the fixture's 2026-27 academic year


class NoticeCase(WorldTestCase):
    """Fixture reminder (school A): Class 5 has sections 5A (class teacher t1; Asha and Bimal) and 5B (math taught by t2;
    Chitra); Class 6 has 6A (science taught by 'dual'; Dev, who has no parent or student login).
    p1 = parent of Asha + Chitra, p2 = parent of Bimal, 'dual' = a parent of Bimal AND a teacher of 6A,
    u_student = Asha's own login, t3 = a teacher with no classes. admin / office / accountant are school-wide.
    Office and admin may post; the accountant may read only."""

    def setUp(self):
        super().setUp()
        patcher = patch("notices.services.today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, author=None, *, title="Notice", body="Please read.", **kwargs):
        if not {"everyone", "role_ids", "grade_ids", "section_ids"} & set(kwargs):
            kwargs["everyone"] = True  # addressed to the whole school unless the test says otherwise
        return services.create_notice(self.school_a, author or self.u_office, title=title, body=body, **kwargs)

    def titles(self, user, school=None, **kw):
        return {n.title for n in services.visible_notices(user, school or self.school_a, **kw)}

    def everyone_but_orphan(self):
        return [self.u_admin, self.u_office, self.u_accountant, self.u_t1, self.u_t2, self.u_t3, self.u_dual,
                self.u_p1, self.u_p2, self.u_student]


class VisibilityTests(NoticeCase):
    def test_a_notice_for_everyone_reaches_everyone_in_the_school(self):
        self.post(title="All")
        for user in self.everyone_but_orphan():
            self.assertEqual(self.titles(user), {"All"}, user.username)

    def test_people_outside_the_school_or_without_a_role_see_nothing(self):
        self.post(title="All")
        self.assertEqual(self.titles(self.u_orphan), set())
        self.assertEqual(self.titles(self.u_pb), set())          # a school-B parent, asking about school A
        self.assertEqual(self.titles(self.u_admin_b), set())     # a school-B admin, asking about school A

    def test_another_schools_notice_never_leaks(self):
        services.create_notice(self.school_b, self.u_admin_b, title="Beta only", body="x", everyone=True)
        self.post(title="Alpha")
        self.assertEqual(self.titles(self.u_admin), {"Alpha"})
        self.assertEqual(self.titles(self.u_admin_b, self.school_b), {"Beta only"})
        self.assertEqual(self.titles(self.u_pb, self.school_b), {"Beta only"})

    def test_a_notice_for_a_role_reaches_people_holding_it(self):
        self.post(title="Parents", role_ids=[self.roles_a["parent"].pk])
        self.post(title="Teachers", role_ids=[self.roles_a["teacher"].pk])
        self.assertEqual(self.titles(self.u_p1), {"Parents"})
        self.assertEqual(self.titles(self.u_p2), {"Parents"})
        self.assertEqual(self.titles(self.u_t1), {"Teachers"})
        self.assertEqual(self.titles(self.u_dual), {"Parents", "Teachers"})  # holds both roles
        self.assertEqual(self.titles(self.u_student), set())
        self.assertEqual(self.titles(self.u_t3), {"Teachers"})

    def test_school_wide_roles_read_everything_whoever_it_was_for(self):
        self.post(title="Class 6 only", grade_ids=[self.grade6.pk])
        for user in (self.u_admin, self.u_office, self.u_accountant):
            self.assertEqual(self.titles(user), {"Class 6 only"}, user.username)

    def test_a_notice_for_a_class_reaches_its_parents_students_and_teachers(self):
        self.post(title="Class 5", grade_ids=[self.grade5.pk])
        for user in (self.u_p1, self.u_p2, self.u_student, self.u_t1, self.u_t2, self.u_dual):  # dual: Bimal's parent
            self.assertEqual(self.titles(user), {"Class 5"}, user.username)
        self.assertEqual(self.titles(self.u_t3), set())  # teaches nothing

    def test_a_notice_for_a_section_reaches_only_that_section(self):
        self.post(title="5B", section_ids=[self.sec5b.pk])
        self.assertEqual(self.titles(self.u_p1), {"5B"})            # Chitra is in 5B
        self.assertEqual(self.titles(self.u_t2), {"5B"})            # teaches 5B
        self.assertEqual(self.titles(self.u_p2), set())             # Bimal is in 5A
        self.assertEqual(self.titles(self.u_student), set())        # Asha is in 5A
        self.assertEqual(self.titles(self.u_t1), set())             # class teacher of 5A, not 5B
        self.assertEqual(self.titles(self.u_dual), set())

    def test_a_teacher_who_is_also_a_parent_reads_both_ways(self):
        self.post(title="6A", section_ids=[self.sec6a.pk])          # they teach 6A
        self.post(title="5A", section_ids=[self.sec5a.pk])          # their child Bimal is in 5A
        self.assertEqual(self.titles(self.u_dual), {"6A", "5A"})

    def test_the_audience_follows_a_student_who_changes_class(self):
        self.post(title="5A", section_ids=[self.sec5a.pk])
        self.assertEqual(self.titles(self.u_p2), {"5A"})
        Enrollment.objects.filter(student=self.bimal).update(section=self.sec5b)
        self.assertEqual(self.titles(self.u_p2), set())             # worked out when asked, nothing to clean up

    def test_withdrawn_and_expired_notices_are_hidden_from_readers(self):
        live = self.post(title="Live")
        gone = self.post(title="Gone")
        services.withdraw_notice(gone, self.u_office)
        self.post(title="Expired", expires_on=TODAY)
        self.post(title="Last day is today", expires_on=TODAY)
        with patch("notices.services.today", return_value=TODAY + timedelta(days=1)):
            self.assertEqual(self.titles(self.u_p1), {"Live"})
        self.assertEqual(self.titles(self.u_p1), {"Live", "Expired", "Last day is today"})
        self.assertEqual(self.titles(self.u_p1, include_dead=True), {"Live", "Gone", "Expired", "Last day is today"})
        self.assertTrue(live.is_live(TODAY))

    def test_an_author_keeps_seeing_their_own_notice(self):
        notice = self.post(self.u_t2, title="Mine", section_ids=[self.sec5b.pk])
        self.assertEqual(self.titles(self.u_t2), {"Mine"})
        from academics.models import TeachingAssignment
        TeachingAssignment.objects.filter(teacher=self.t2).delete()  # no longer teaches 5B
        self.assertEqual(self.titles(self.u_t2), {"Mine"})
        self.assertEqual(notice.author, self.u_t2)

    def test_losing_the_read_permission_or_the_membership_hides_everything(self):
        self.post(title="All")
        role = self.roles_a["parent"]
        role.permissions = [p for p in role.permissions if p != "notices.view"]
        role.save()
        self.assertEqual(self.titles(self.u_p1), set())
        self.assertFalse(services.can_view(self.u_p1, self.school_a))
        Membership.objects.filter(user=self.u_p2).update(is_active=False)
        self.assertEqual(self.titles(self.u_p2), set())

    def test_get_visible_notice_hides_withdrawn_ones_from_everyone_but_managers(self):
        notice = self.post(self.u_t1, title="Mine", section_ids=[self.sec5a.pk])
        services.withdraw_notice(notice, self.u_t1)
        self.assertIsNone(services.get_visible_notice(self.u_p1, self.school_a, notice.pk))
        self.assertEqual(services.get_visible_notice(self.u_t1, self.school_a, notice.pk), notice)      # the author
        self.assertEqual(services.get_visible_notice(self.u_office, self.school_a, notice.pk), notice)  # the office
        self.assertIsNone(services.get_visible_notice(self.u_t2, self.school_a, notice.pk))             # another teacher
        self.assertIsNone(services.get_visible_notice(self.u_p1, self.school_a, 999999))

    def test_a_notice_of_another_school_cannot_be_opened_by_id(self):
        other = services.create_notice(self.school_b, self.u_admin_b, title="Beta", body="x", everyone=True)
        self.assertIsNone(services.get_visible_notice(self.u_admin, self.school_a, other.pk))

    def test_visibility_does_not_cost_a_query_per_notice(self):
        for n in range(15):
            self.post(title=f"N{n}", grade_ids=[self.grade5.pk])
        with CaptureQueriesContext(connection) as small:
            list(services.with_audience(services.visible_notices(self.u_p1, self.school_a)))
        for n in range(15):
            self.post(title=f"M{n}", section_ids=[self.sec5a.pk])
        with CaptureQueriesContext(connection) as big:
            rows = list(services.with_audience(services.visible_notices(self.u_p1, self.school_a)))
            for notice in rows:
                notice.audience_labels()
        self.assertEqual(len(rows), 30)
        self.assertLessEqual(len(big), len(small) + 1)


class WhoMayPostTests(NoticeCase):
    def audience(self, user):
        return services.postable_audience(user, self.school_a)

    def test_school_wide_posters_may_address_anyone_in_their_school(self):
        for user in (self.u_admin, self.u_office):
            audience = self.audience(user)
            self.assertTrue(audience.everyone)
            self.assertEqual(audience.grades.count(), 2)
            self.assertEqual(audience.sections.count(), 3)
            self.assertEqual(audience.roles.count(), 6)
            self.assertTrue(services.can_post(user, self.school_a))

    def test_a_teacher_may_address_only_the_sections_they_teach(self):
        for user, section in ((self.u_t1, self.sec5a), (self.u_t2, self.sec5b), (self.u_dual, self.sec6a)):
            audience = self.audience(user)
            self.assertFalse(audience.everyone)
            self.assertEqual(list(audience.sections), [section])
            self.assertEqual(audience.grades.count() + audience.roles.count(), 0)

    def test_people_who_cannot_post(self):
        self.assertFalse(services.can_post(self.u_accountant, self.school_a))  # may read, not write
        self.assertFalse(services.can_post(self.u_p1, self.school_a))
        self.assertFalse(services.can_post(self.u_student, self.school_a))
        self.assertFalse(services.can_post(self.u_orphan, self.school_a))
        self.assertFalse(services.can_post(self.u_t3, self.school_a))          # allowed to post, but teaches no class
        self.assertFalse(services.can_post(self.u_admin_b, self.school_a))     # an admin of another school

    def test_a_parent_role_that_was_given_post_still_has_nobody_to_address(self):
        role = self.roles_a["parent"]
        role.permissions = list(role.permissions) + ["notices.post"]
        role.save()
        self.assertFalse(services.can_post(self.u_p1, self.school_a))
        with self.assertRaises(NoticeError):
            services.create_notice(self.school_a, self.u_p1, title="Hi", body="x", everyone=True)


class CreateNoticeTests(NoticeCase):
    def test_a_notice_for_everyone_stores_one_target(self):
        notice = self.post(everyone=True, role_ids=[self.roles_a["parent"].pk], grade_ids=[self.grade5.pk])
        kinds = list(notice.targets.values_list("kind", flat=True))
        self.assertEqual(kinds, ["school"])  # "everyone" already includes the rest
        self.assertEqual(notice.audience_labels(), ["Everyone"])

    def test_targets_for_roles_classes_and_sections(self):
        notice = self.post(
            role_ids=[self.roles_a["teacher"].pk], grade_ids=[self.grade6.pk, self.grade5.pk], section_ids=[self.sec5b.pk, self.sec5b.pk]
        )
        self.assertEqual(notice.targets.count(), 4)
        self.assertEqual(notice.audience_labels(), ["Teacher", "Class 5", "Class 6", "Class 5-B"])
        self.assertTrue(all(t.school_id == self.school_a.pk for t in notice.targets.all()))

    def test_values_may_arrive_as_text_from_a_form(self):
        notice = self.post(section_ids=[str(self.sec5a.pk)])
        self.assertEqual(notice.targets.get().section_id, self.sec5a.pk)

    def test_the_author_and_time_are_recorded(self):
        before = timezone.now()
        notice = self.post(self.u_office, title="  Annual Day  ", body="  Come along!  ", category="event")
        self.assertEqual((notice.title, notice.body, notice.category), ("Annual Day", "Come along!", "event"))
        self.assertEqual(notice.author, self.u_office)
        self.assertEqual(notice.author_name, "office_a")  # no full name on the fixture: falls back to the username
        self.assertEqual(notice.school, self.school_a)
        self.assertGreaterEqual(notice.posted_at, before)
        self.assertIsNone(notice.edited_at)
        self.assertFalse(notice.is_withdrawn)

    def test_the_author_name_is_kept_if_the_account_goes(self):
        self.u_t1.first_name, self.u_t1.last_name = "Tara", "Nair"
        self.u_t1.save()
        notice = self.post(self.u_t1, section_ids=[self.sec5a.pk])
        self.u_t1.delete()
        notice.refresh_from_db()
        self.assertIsNone(notice.author)
        self.assertEqual(notice.author_name, "Tara Nair")

    def test_a_teacher_can_post_to_their_own_section_only(self):
        self.post(self.u_t1, section_ids=[self.sec5a.pk])
        for bad in (
            {"section_ids": [self.sec5b.pk]},
            {"section_ids": [self.sec5a.pk, self.sec6a.pk]},
            {"everyone": True},
            {"grade_ids": [self.grade5.pk]},
            {"role_ids": [self.roles_a["parent"].pk]},
        ):
            with self.assertRaises(NoticeError, msg=str(bad)):
                self.post(self.u_t1, **bad)
        self.assertEqual(Notice.objects.count(), 1)

    def test_people_without_the_permission_cannot_post(self):
        for user in (self.u_accountant, self.u_p1, self.u_student, self.u_orphan, self.u_admin_b):
            with self.assertRaises(NoticeError, msg=user.username):
                self.post(user, everyone=True)
        self.assertEqual(Notice.objects.count(), 0)

    def test_another_schools_classes_roles_and_sections_are_refused(self):
        beta_role = self.roles_b["parent"]
        for bad in (
            {"role_ids": [beta_role.pk]}, {"grade_ids": [self.grade5_b.pk]}, {"section_ids": [self.sec_b.pk]},
            {"section_ids": [999999]},
        ):
            with self.assertRaises(NoticeError, msg=str(bad)):
                self.post(self.u_admin, **bad)
        self.assertEqual(Notice.objects.count(), 0)

    def test_the_text_is_checked(self):
        for kwargs, fragment in (
            ({"title": "   "}, "title"),
            ({"title": "x" * 121}, "120 characters"),
            ({"body": "  "}, "write the notice"),
            ({"body": "x" * 4001}, "4000 characters"),
            ({"category": "gossip"}, "category"),
            ({"expires_on": TODAY - timedelta(days=1)}, "in the past"),
        ):
            with self.assertRaises(NoticeError, msg=str(kwargs)) as caught:
                self.post(**kwargs)
            self.assertIn(fragment, str(caught.exception))
        self.post(title="x" * 120, body="x" * 4000, expires_on=TODAY)  # exactly at the limits is fine

    def test_an_audience_is_required_and_nothing_is_saved_without_one(self):
        with self.assertRaises(NoticeError) as caught:
            services.create_notice(self.school_a, self.u_office, title="T", body="B")
        self.assertIn("who the notice is for", str(caught.exception))
        self.assertEqual(Notice.objects.count(), 0)

    def test_a_target_cannot_point_into_another_school(self):
        notice = self.post()
        with self.assertRaises(ValidationError):
            NoticeTarget(school=self.school_a, notice=notice, kind="grade", grade=self.grade5_b).save()
        with self.assertRaises(ValidationError):
            NoticeTarget(school=self.school_a, notice=notice, kind="role", role=self.roles_b["parent"]).save()
        with self.assertRaises(ValidationError):
            NoticeTarget(school=self.school_b, notice=notice, kind="school").save()

    def test_the_database_insists_a_target_has_exactly_the_column_for_its_kind(self):
        notice = self.post()
        for kwargs in (
            {"kind": "school", "grade": self.grade5},
            {"kind": "grade"},
            {"kind": "section", "grade": self.grade5},
            {"kind": "role", "role": self.roles_a["parent"], "section": self.sec5a},
        ):
            with self.assertRaises(IntegrityError, msg=str(kwargs)), transaction.atomic():
                NoticeTarget(school=self.school_a, notice=notice, **kwargs).save()


class ManageNoticeTests(NoticeCase):
    def setUp(self):
        super().setUp()
        self.teachers = self.post(self.u_t1, title="By t1", section_ids=[self.sec5a.pk])
        self.office = self.post(self.u_office, title="By office")

    def test_school_wide_posters_manage_any_notice(self):
        for user in (self.u_office, self.u_admin):
            for notice in (self.teachers, self.office):
                self.assertTrue(services.can_manage_notice(user, self.school_a, notice))

    def test_a_teacher_manages_only_their_own(self):
        self.assertTrue(services.can_manage_notice(self.u_t1, self.school_a, self.teachers))
        self.assertFalse(services.can_manage_notice(self.u_t1, self.school_a, self.office))
        self.assertFalse(services.can_manage_notice(self.u_t2, self.school_a, self.teachers))

    def test_readers_and_outsiders_manage_nothing(self):
        for user in (self.u_accountant, self.u_p1, self.u_student, self.u_orphan, self.u_admin_b):
            self.assertFalse(services.can_manage_notice(user, self.school_a, self.office), user.username)

    def test_an_author_who_lost_the_post_permission_manages_nothing(self):
        role = self.roles_a["teacher"]
        role.permissions = [p for p in role.permissions if p != "notices.post"]
        role.save()
        fresh = User.objects.get(pk=self.u_t1.pk)  # roles are looked up once per user object
        self.assertFalse(services.can_manage_notice(fresh, self.school_a, self.teachers))

    def test_another_schools_admin_cannot_manage_it(self):
        self.assertFalse(services.can_manage_notice(self.u_admin_b, self.school_b, self.office))
        self.assertFalse(services.can_manage_notice(self.u_admin, self.school_b, self.office))

    def test_editing_changes_the_words_but_not_the_audience(self):
        services.update_notice(self.teachers, self.u_t1, title="New title", body="New body", category="urgent", expires_on=TODAY)
        self.teachers.refresh_from_db()
        self.assertEqual((self.teachers.title, self.teachers.body, self.teachers.category), ("New title", "New body", "urgent"))
        self.assertEqual(self.teachers.expires_on, TODAY)
        self.assertIsNotNone(self.teachers.edited_at)
        self.assertEqual(self.teachers.audience_labels(), ["Class 5-A"])
        self.assertEqual(self.teachers.author, self.u_t1)  # the office fixing a typo does not take over authorship
        services.update_notice(self.teachers, self.u_office, title="Typo fixed", body="Body", category="general")
        self.teachers.refresh_from_db()
        self.assertEqual((self.teachers.title, self.teachers.author), ("Typo fixed", self.u_t1))

    def test_editing_is_checked_like_posting(self):
        with self.assertRaises(NoticeError):
            services.update_notice(self.teachers, self.u_t1, title="", body="b", category="general")
        with self.assertRaises(NoticeError):
            services.update_notice(self.teachers, self.u_t1, title="t", body="b", category="general", expires_on=TODAY - timedelta(days=1))
        with self.assertRaises(NoticeError):
            services.update_notice(self.office, self.u_t1, title="t", body="b", category="general")  # not theirs
        self.office.refresh_from_db()
        self.assertEqual(self.office.title, "By office")

    def test_withdrawing_hides_it_and_cannot_be_edited_afterwards(self):
        services.withdraw_notice(self.teachers, self.u_t1)
        self.teachers.refresh_from_db()
        self.assertTrue(self.teachers.is_withdrawn)
        self.assertNotIn("By t1", self.titles(self.u_p1))
        with self.assertRaises(NoticeError):
            services.update_notice(self.teachers, self.u_t1, title="t", body="b", category="general")

    def test_withdrawing_twice_keeps_the_first_time(self):
        services.withdraw_notice(self.office, self.u_office)
        first = Notice.objects.get(pk=self.office.pk).withdrawn_at
        services.withdraw_notice(self.office, self.u_admin)
        self.assertEqual(Notice.objects.get(pk=self.office.pk).withdrawn_at, first)

    def test_only_managers_can_withdraw(self):
        for user in (self.u_t2, self.u_p1, self.u_accountant, self.u_admin_b):
            with self.assertRaises(NoticeError, msg=user.username):
                services.withdraw_notice(self.office, user)
        self.office.refresh_from_db()
        self.assertFalse(self.office.is_withdrawn)


class ReadTrackingTests(NoticeCase):
    def test_marking_read_is_remembered_once(self):
        notice = self.post()
        services.mark_read(notice, self.u_p1)
        services.mark_read(notice, self.u_p1)
        self.assertEqual(NoticeRead.objects.filter(notice=notice, user=self.u_p1).count(), 1)
        self.assertEqual(NoticeRead.objects.get().school, self.school_a)

    def test_unread_notices_exclude_read_own_old_and_withdrawn_ones(self):
        read = self.post(self.u_admin, title="Read")
        self.post(self.u_admin, title="Unread")
        self.post(self.u_office, title="Mine")
        withdrawn = self.post(self.u_admin, title="Withdrawn")
        old = self.post(self.u_admin, title="Old")
        Notice.objects.filter(pk=old.pk).update(posted_at=timezone.now() - timedelta(days=services.UNREAD_WINDOW_DAYS + 1))
        services.withdraw_notice(withdrawn, self.u_admin)
        services.mark_read(read, self.u_p1)
        self.assertEqual({n.title for n in services.unread_notices(self.u_p1, self.school_a)}, {"Unread", "Mine"})
        self.assertEqual({n.title for n in services.unread_notices(self.u_office, self.school_a)}, {"Unread", "Read"})

    def test_unread_counts_only_what_the_person_may_read(self):
        self.post(title="5B", section_ids=[self.sec5b.pk])
        self.assertEqual(services.unread_notices(self.u_p1, self.school_a).count(), 1)
        self.assertEqual(services.unread_notices(self.u_p2, self.school_a).count(), 0)
        self.assertEqual(services.unread_notices(self.u_orphan, self.school_a).count(), 0)

    def test_a_class_notice_reach(self):
        notice = self.post(grade_ids=[self.grade5.pk])
        found = services.reach(notice)
        # Asha's login, p1, p2, dual (Bimal's other parent), the class teacher of 5A and the math teacher of 5B.
        self.assertEqual((found.total, found.read, found.unreached), (6, 0, 0))
        services.mark_read(notice, self.u_p1)
        services.mark_read(notice, self.u_t2)
        services.mark_read(notice, self.u_t3)  # not in the audience: does not count
        self.assertEqual(services.reach(notice).read, 2)

    def test_reach_counts_students_nobody_can_reach(self):
        notice = self.post(grade_ids=[self.grade5.pk])
        loner = Student.objects.create(school=self.school_a, admission_no="A9", first_name="Loner")
        Enrollment.objects.create(school=self.school_a, student=loner, section=self.sec5a, roll_no=9)
        self.assertEqual(services.reach(notice).unreached, 1)
        self.assertEqual(services.reach(notice).total, 6)  # nobody new to tell

    def test_a_whole_school_notice_reach(self):
        notice = self.post(self.u_office)
        found = services.reach(notice)
        # Everyone with a login except the author and the person with no role; Dev (class 6) has nobody who can be told.
        self.assertEqual(found.total, 9)
        self.assertEqual(found.unreached, 1)

    def test_a_role_notice_reach_has_no_student_count(self):
        notice = self.post(role_ids=[self.roles_a["parent"].pk])
        found = services.reach(notice)
        self.assertEqual((found.total, found.unreached), (3, 0))  # p1, p2, dual

    def test_deactivated_people_are_not_counted(self):
        notice = self.post(role_ids=[self.roles_a["parent"].pk])
        self.u_p2.is_active = False
        self.u_p2.save()
        Membership.objects.filter(user=self.u_dual, role=self.roles_a["parent"]).update(is_active=False)
        self.assertEqual(services.reach(notice).total, 1)

    def test_the_author_is_never_counted(self):
        notice = self.post(self.u_t1, section_ids=[self.sec5a.pk])
        # 5A: Asha (her login + p1) and Bimal (p2, dual); the class teacher t1 wrote it.
        self.assertEqual(services.reach(notice).total, 4)

    def test_a_removed_section_leaves_a_notice_with_no_audience(self):
        notice = self.post(self.u_office, section_ids=[self.sec6a.pk])
        from academics.models import TeachingAssignment
        Enrollment.objects.filter(section=self.sec6a).delete()
        TeachingAssignment.objects.filter(section=self.sec6a).delete()
        self.sec6a.delete()
        notice = Notice.objects.get(pk=notice.pk)
        self.assertEqual(services.reach(notice).total, 0)
        self.assertEqual(notice.audience_labels(), ["No one (the classes it was sent to were removed)"])
