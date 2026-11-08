from django.core.exceptions import ValidationError
from django.urls import reverse

from academics.models import AcademicYear, Enrollment
from accounts.access import has_perm, permissions_for, scopes_for, sections_in_scope, students_in_scope, visible_students
from accounts.models import Membership, Role, normalize_phone
from core.testing import PASSWORD, WorldTestCase
from people.models import Student


class ScopedVisibilityTests(WorldTestCase):
    """Who can see which students: the heart of the role-based access control."""

    def test_admin_sees_every_student_of_own_school_only(self):
        seen = self.admission_nos(visible_students(self.u_admin, self.school_a))
        self.assertEqual(seen, {"A1", "A2", "A3", "A4"})
        self.assertNotIn("B1", seen)

    def test_class_teacher_sees_only_their_section(self):
        self.assertEqual(self.admission_nos(visible_students(self.u_t1, self.school_a)), {"A1", "A2"})

    def test_subject_teacher_sees_the_section_they_teach(self):
        self.assertEqual(self.admission_nos(visible_students(self.u_t2, self.school_a)), {"A3"})

    def test_teacher_with_no_assignment_sees_nobody(self):
        self.assertEqual(self.admission_nos(visible_students(self.u_t3, self.school_a)), set())

    def test_parent_sees_only_own_children_including_siblings(self):
        self.assertEqual(self.admission_nos(visible_students(self.u_p1, self.school_a)), {"A1", "A3"})
        self.assertEqual(self.admission_nos(visible_students(self.u_p2, self.school_a)), {"A2"})

    def test_student_sees_only_themselves(self):
        self.assertEqual(self.admission_nos(visible_students(self.u_student, self.school_a)), {"A1"})

    def test_user_with_two_roles_gets_the_union(self):
        # teacher of 6A (Dev) + parent of Bimal
        self.assertEqual(self.admission_nos(visible_students(self.u_dual, self.school_a)), {"A2", "A4"})

    def test_dashboard_panels_can_ask_for_one_scope_at_a_time(self):
        self.assertEqual(self.admission_nos(students_in_scope(self.u_dual, self.school_a, "children")), {"A2"})
        self.assertEqual(self.admission_nos(students_in_scope(self.u_dual, self.school_a, "assigned")), {"A4"})

    def test_a_role_only_counts_for_permissions_it_actually_grants(self):
        # parents may view students but cannot mark attendance
        self.assertEqual(self.admission_nos(visible_students(self.u_p1, self.school_a, "students.view")), {"A1", "A3"})
        self.assertEqual(self.admission_nos(visible_students(self.u_p1, self.school_a, "attendance.mark")), set())

    def test_no_double_rows_when_two_paths_lead_to_the_same_student(self):
        # t1 reaches Asha as class teacher; make t1 also teach her section a subject
        from academics.models import TeachingAssignment

        TeachingAssignment.objects.create(school=self.school_a, section=self.sec5a, subject=self.math, teacher=self.t1)
        qs = visible_students(self.u_t1, self.school_a)
        self.assertEqual(qs.count(), 2)
        self.assertEqual(len(list(qs)), 2)

    def test_deactivated_membership_loses_access(self):
        Membership.objects.filter(user=self.u_p1).update(is_active=False)
        self.assertEqual(self.admission_nos(visible_students(self.u_p1, self.school_a)), set())
        self.assertFalse(has_perm(self.u_p1, self.school_a, "students.view"))

    def test_staff_profile_deactivated_removes_teacher_scope(self):
        self.t1.is_active = False
        self.t1.save()
        self.assertEqual(self.admission_nos(visible_students(self.u_t1, self.school_a)), set())

    def test_moving_a_student_to_a_new_year_section_changes_teacher_scope(self):
        # Scope uses the CURRENT year only: a past-year enrolment must not grant access.
        AcademicYear.objects.filter(pk=self.year_a.pk).update(is_current=False)
        self.assertEqual(self.admission_nos(visible_students(self.u_t1, self.school_a)), set())


class SectionsInScopeTests(WorldTestCase):
    """Which class sections a scope connects a person to (used to decide who is in a notice's audience)."""

    def names(self, user, scope, school=None):
        return {str(s) for s in sections_in_scope(user, school or self.school_a, scope)}

    def test_school_wide_sees_every_current_section_of_the_school_only(self):
        self.assertEqual(len(self.names(self.u_admin, "all")), 3)
        self.assertEqual(len(self.names(self.u_admin_b, "all", self.school_b)), 1)

    def test_a_teacher_gets_the_sections_they_teach_or_are_class_teacher_of(self):
        self.assertEqual({s.pk for s in sections_in_scope(self.u_t1, self.school_a, "assigned")}, {self.sec5a.pk})
        self.assertEqual({s.pk for s in sections_in_scope(self.u_t2, self.school_a, "assigned")}, {self.sec5b.pk})
        self.assertEqual({s.pk for s in sections_in_scope(self.u_dual, self.school_a, "assigned")}, {self.sec6a.pk})
        self.assertEqual(self.names(self.u_t3, "assigned"), set())  # teaches nothing yet

    def test_a_parent_gets_the_sections_their_children_are_in(self):
        self.assertEqual({s.pk for s in sections_in_scope(self.u_p1, self.school_a, "children")}, {self.sec5a.pk, self.sec5b.pk})
        self.assertEqual({s.pk for s in sections_in_scope(self.u_p2, self.school_a, "children")}, {self.sec5a.pk})

    def test_a_student_gets_their_own_section(self):
        self.assertEqual({s.pk for s in sections_in_scope(self.u_student, self.school_a, "self")}, {self.sec5a.pk})

    def test_a_scope_the_person_has_no_link_for_gives_nothing(self):
        self.assertEqual(self.names(self.u_p1, "assigned"), set())
        self.assertEqual(self.names(self.u_t1, "children"), set())
        self.assertEqual(self.names(self.u_orphan, "self"), set())

    def test_other_schools_and_past_years_never_leak_in(self):
        self.assertEqual(self.names(self.u_pb, "children"), set())  # parent of a school-B child, asked about school A
        AcademicYear.objects.filter(pk=self.year_a.pk).update(is_current=False)
        self.assertEqual(self.names(self.u_p1, "children"), set())
        self.assertEqual(self.names(self.u_admin, "all"), set())

    def test_a_deactivated_staff_profile_loses_their_sections(self):
        self.t1.is_active = False
        self.t1.save()
        self.assertEqual(self.names(self.u_t1, "assigned"), set())

    def test_one_section_is_not_listed_twice(self):
        sections = list(sections_in_scope(self.u_p1, self.school_a, "children"))
        self.assertEqual(len(sections), len({s.pk for s in sections}))


class SchoolIsolationTests(WorldTestCase):
    def test_admin_of_one_school_has_no_rights_in_another(self):
        self.assertFalse(has_perm(self.u_admin, self.school_b, "students.view"))
        self.assertEqual(self.admission_nos(visible_students(self.u_admin, self.school_b)), set())
        self.assertEqual(self.admission_nos(visible_students(self.u_admin_b, self.school_b)), {"B1"})

    def test_a_membership_cannot_disagree_with_its_roles_school(self):
        m = Membership.objects.create(user=self.u_orphan, role=self.roles_b["parent"])
        self.assertEqual(m.school_id, self.school_b.id)

    def test_enrolment_cannot_mix_schools(self):
        bad = Enrollment(school=self.school_a, student=self.zoya, section=self.sec5a)
        with self.assertRaises(ValidationError):
            bad.save()

    def test_only_one_current_academic_year_per_school(self):
        newer = AcademicYear.objects.create(
            school=self.school_a, name="2027-28", start_date=self.year_a.end_date, end_date=self.year_a.end_date, is_current=True
        )
        self.year_a.refresh_from_db()
        self.assertFalse(self.year_a.is_current)
        self.assertTrue(newer.is_current)
        self.assertTrue(AcademicYear.objects.get(pk=self.year_b.pk).is_current)  # other school untouched

    def test_double_enrolment_in_one_year_is_rejected(self):
        dup = Enrollment(school=self.school_a, student=self.asha, section=self.sec5b)
        with self.assertRaises(ValidationError):
            dup.full_clean()


class PermissionVocabularyTests(WorldTestCase):
    def test_default_roles_only_use_known_permissions(self):
        for role in Role.objects.all():
            role.full_clean()

    def test_unknown_permission_code_is_rejected(self):
        role = self.roles_a["teacher"]
        role.permissions = ["students.view", "teleport.everywhere"]
        with self.assertRaises(ValidationError):
            role.full_clean()

    def test_scopes_and_permissions_helpers(self):
        self.assertEqual(scopes_for(self.u_dual, self.school_a), {"assigned", "children"})
        self.assertIn("attendance.mark", permissions_for(self.u_t1, self.school_a))
        self.assertNotIn("attendance.mark", permissions_for(self.u_p1, self.school_a))
        self.assertTrue(has_perm(self.u_admin, self.school_a, "fees.collect", all_scope=True))
        self.assertFalse(has_perm(self.u_t1, self.school_a, "students.view", all_scope=True))


class LoginTests(WorldTestCase):
    def login(self, identifier, password=PASSWORD):
        return self.client.post(reverse("login"), {"username": identifier, "password": password})

    def assertLoggedIn(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_username_login(self):
        self.assertLoggedIn(self.login("t1"))

    def test_username_is_case_insensitive(self):
        self.assertLoggedIn(self.login("ADMIN_A"))

    def test_phone_login_in_several_formats(self):
        for form in ["9876543210", "+91 98765 43210", "98765-43210", "+919876543210"]:
            self.client.logout()
            self.assertLoggedIn(self.login(form))

    def test_email_login(self):
        self.u_p2.email = "paul@example.com"
        self.u_p2.save()
        self.assertLoggedIn(self.login("Paul@Example.com"))

    def test_wrong_password_is_refused(self):
        response = self.login("t1", "nope")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unknown_identifier_is_refused(self):
        self.assertEqual(self.login("0000000000").status_code, 200)

    def test_account_without_a_school_role_cannot_sign_in(self):
        response = self.login("orphan")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not linked to a school")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_account_is_refused(self):
        self.u_t1.is_active = False
        self.u_t1.save()
        self.assertEqual(self.login("t1").status_code, 200)

    def test_phone_numbers_are_normalised(self):
        self.assertEqual(normalize_phone("+91 98765-43210"), "9876543210")
        self.assertEqual(normalize_phone(None), "")
        self.assertEqual(normalize_phone("n/a"), "")


class TenantAdminTests(WorldTestCase):
    """Office staff use the Django admin for data entry: it must be locked to their school and role."""

    def test_office_staff_see_only_their_schools_students(self):
        self.client.force_login(self.u_office)
        response = self.client.get(reverse("admin:people_student_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asha")
        self.assertNotContains(response, "Zoya")

    def test_teachers_cannot_enter_the_admin(self):
        self.client.force_login(self.u_t1)  # is_staff is False
        response = self.client.get(reverse("admin:people_student_changelist"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_staff_user_with_a_non_school_wide_role_gets_nothing(self):
        self.u_t1.is_staff = True
        self.u_t1.save()
        self.client.force_login(self.u_t1)
        response = self.client.get(reverse("admin:people_student_changelist"))
        self.assertEqual(response.status_code, 403)

    def test_accountant_can_view_students_but_not_add(self):
        self.client.force_login(self.u_accountant)
        self.assertEqual(self.client.get(reverse("admin:people_student_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:people_student_add")).status_code, 403)

    def test_school_is_filled_in_and_cannot_be_forged(self):
        self.client.force_login(self.u_office)
        response = self.client.post(
            reverse("admin:people_student_add"),
            {"first_name": "Nina", "last_name": "New", "admission_no": "A9", "status": "active", "school": self.school_b.pk},
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["adminform"].form.errors)
        self.assertEqual(Student.objects.get(admission_no="A9").school, self.school_a)

    def test_dropdowns_only_offer_the_schools_own_rows(self):
        self.client.force_login(self.u_office)
        response = self.client.get(reverse("admin:people_studentguardian_add"))
        self.assertContains(response, "Asha")
        self.assertNotContains(response, "Zoya")

    def test_cannot_open_another_schools_record_by_guessing_the_id(self):
        self.client.force_login(self.u_office)
        response = self.client.get(reverse("admin:people_student_change", args=[self.zoya.pk]))
        self.assertIn(response.status_code, (302, 404))  # admin redirects "not found" to the index

    def test_only_the_platform_owner_manages_schools_and_users(self):
        self.client.force_login(self.u_admin)  # principal, not superuser
        self.assertEqual(self.client.get(reverse("admin:core_school_changelist")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:accounts_user_changelist")).status_code, 403)
