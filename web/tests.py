import json

from django.contrib.auth import get_user_model
from django.urls import reverse

from core.testing import PASSWORD, WorldTestCase


class AnonymousAccessTests(WorldTestCase):
    def test_every_data_page_redirects_to_login(self):
        for name in ["dashboard", "student_list", "fee_structure"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse("login"), response["Location"], name)
        response = self.client.get(reverse("student_detail", args=[self.asha.pk]))
        self.assertEqual(response.status_code, 302)

    def test_login_page_is_public(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Phone number, email or username")

    def test_logging_out_needs_a_post(self):
        self.client.force_login(self.u_admin)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)


class DashboardTests(WorldTestCase):
    def get(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("dashboard"))

    def test_admin_sees_school_wide_numbers(self):
        response = self.get(self.u_admin)
        self.assertContains(response, "Active students")
        self.assertEqual(response.context["school_stats"]["students"], 4)  # Zoya belongs to another school
        self.assertNotIn("children", response.context)

    def test_teacher_sees_their_classes(self):
        response = self.get(self.u_t1)
        self.assertContains(response, "Your classes")
        self.assertContains(response, "Class teacher")
        self.assertNotContains(response, "Active students")

    def test_parent_sees_their_children(self):
        response = self.get(self.u_p1)
        names = {c.first_name for c in response.context["children"]}
        self.assertEqual(names, {"Asha", "Chitra"})

    def test_student_sees_own_profile(self):
        response = self.get(self.u_student)
        self.assertEqual([s.first_name for s in response.context["me"]], ["Asha"])

    def test_dual_role_user_gets_both_panels(self):
        response = self.get(self.u_dual)
        self.assertContains(response, "Your classes")
        self.assertEqual([c.first_name for c in response.context["children"]], ["Bimal"])

    def test_navigation_wording_follows_the_role(self):
        self.assertContains(self.get(self.u_admin), ">Students<")
        self.assertContains(self.get(self.u_t1), ">My students<")
        self.assertContains(self.get(self.u_p1), ">My children<")
        self.assertContains(self.get(self.u_student), ">My profile<")

    def test_admin_link_only_for_staff_accounts(self):
        self.assertContains(self.get(self.u_office), "Manage data")
        self.assertNotContains(self.get(self.u_p1), "Manage data")

    def test_superuser_without_a_school_gets_a_clear_message(self):
        owner = get_user_model().objects.create_superuser("owner", password=PASSWORD)
        response = self.get(owner)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "No school linked", status_code=403)


class StudentListTests(WorldTestCase):
    def names(self, user, **params):
        self.client.force_login(user)
        response = self.client.get(reverse("student_list"), params)
        self.assertEqual(response.status_code, 200)
        return {s.first_name for s in response.context["page"].object_list}

    def test_each_role_gets_exactly_their_own_slice(self):
        self.assertEqual(self.names(self.u_admin), {"Asha", "Bimal", "Chitra", "Dev"})
        self.assertEqual(self.names(self.u_t1), {"Asha", "Bimal"})
        self.assertEqual(self.names(self.u_t2), {"Chitra"})
        self.assertEqual(self.names(self.u_p1), {"Asha", "Chitra"})
        self.assertEqual(self.names(self.u_student), {"Asha"})
        self.assertEqual(self.names(self.u_t3), set())

    def test_search(self):
        self.assertEqual(self.names(self.u_admin, q="chit"), {"Chitra"})
        self.assertEqual(self.names(self.u_admin, q="A4"), {"Dev"})  # by admission number
        self.assertEqual(self.names(self.u_t1, q="Dev"), set())  # exists, but not theirs

    def test_class_filter(self):
        self.assertEqual(self.names(self.u_admin, section=self.sec5b.pk), {"Chitra"})

    def test_class_filter_cannot_be_used_to_peek_into_other_sections(self):
        # t1 asks for 6A (Dev's class) by id: ignored, they still only see their own students
        self.assertEqual(self.names(self.u_t1, section=self.sec6a.pk), {"Asha", "Bimal"})

    def test_garbage_filter_values_are_harmless(self):
        self.assertEqual(self.names(self.u_t1, section="abc"), {"Asha", "Bimal"})
        self.assertEqual(self.names(self.u_t1, section="999999", page="zzz"), {"Asha", "Bimal"})

    def test_list_is_ordered_by_class_then_roll(self):
        self.client.force_login(self.u_admin)
        response = self.client.get(reverse("student_list"))
        order = [s.first_name for s in response.context["page"].object_list]
        self.assertEqual(order, ["Asha", "Bimal", "Chitra", "Dev"])

    def test_query_count_does_not_grow_with_the_number_of_students(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from academics.models import Enrollment
        from people.models import Student

        self.client.force_login(self.u_admin)
        self.client.get(reverse("student_list"))  # warm-up: the first request also saves the session

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(reverse("student_list"))
            self.assertEqual(response.status_code, 200)
            return len(ctx)

        before = count_queries()
        for i in range(25):
            s = Student.objects.create(school=self.school_a, admission_no=f"X{i}", first_name=f"Extra{i}")
            Enrollment.objects.create(school=self.school_a, student=s, section=self.sec5a, roll_no=10 + i)
        self.assertEqual(count_queries(), before)
        self.assertLessEqual(before, 10)  # includes the two counts behind the navigation's "new" badge


class StudentDetailTests(WorldTestCase):
    def status(self, user, student):
        self.client.force_login(user)
        return self.client.get(reverse("student_detail", args=[student.pk])).status_code

    def test_parent_can_open_own_child_but_not_someone_elses(self):
        self.assertEqual(self.status(self.u_p1, self.asha), 200)
        self.assertEqual(self.status(self.u_p1, self.chitra), 200)  # sibling via the same guardian
        self.assertEqual(self.status(self.u_p1, self.bimal), 404)
        self.assertEqual(self.status(self.u_p2, self.asha), 404)

    def test_teacher_limited_to_their_sections(self):
        self.assertEqual(self.status(self.u_t1, self.bimal), 200)
        self.assertEqual(self.status(self.u_t1, self.dev), 404)
        self.assertEqual(self.status(self.u_t1, self.chitra), 404)

    def test_student_can_only_open_themselves(self):
        self.assertEqual(self.status(self.u_student, self.asha), 200)
        self.assertEqual(self.status(self.u_student, self.bimal), 404)

    def test_other_schools_children_do_not_exist_for_you(self):
        self.assertEqual(self.status(self.u_admin, self.zoya), 404)
        self.assertEqual(self.status(self.u_admin_b, self.asha), 404)
        self.assertEqual(self.status(self.u_admin_b, self.zoya), 200)

    def test_detail_shows_guardians_and_class(self):
        self.client.force_login(self.u_admin)
        response = self.client.get(reverse("student_detail", args=[self.bimal.pk]))
        self.assertContains(response, "Paul")  # both of Bimal's guardians
        self.assertContains(response, "Dual")
        self.assertContains(response, "Class 5-A")


class FeeStructureTests(WorldTestCase):
    def get(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("fee_structure"))

    def test_admin_sees_every_class(self):
        response = self.get(self.u_admin)
        self.assertContains(response, "Class 5")
        self.assertContains(response, "Class 6")

    def test_parent_sees_only_their_childrens_classes(self):
        response = self.get(self.u_p1)  # children are both in Class 5
        self.assertContains(response, "Class 5")
        self.assertNotContains(response, "Class 6")
        self.assertContains(response, "12,000")  # 1000 x 12 months

    def test_student_role_has_no_fee_access(self):
        self.assertEqual(self.get(self.u_student).status_code, 403)

    def test_teacher_has_no_fee_access(self):
        self.assertEqual(self.get(self.u_t1).status_code, 403)

    def test_fees_of_other_schools_never_appear(self):
        self.assertNotContains(self.get(self.u_admin_b), "Tuition Fee")


class PwaTests(WorldTestCase):
    def test_manifest_is_valid_and_installable(self):
        response = self.client.get(reverse("manifest"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        manifest = json.loads(response.content)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({i["sizes"] for i in manifest["icons"]}, {"192x192", "512x512"})

    def test_service_worker_is_root_scoped_and_never_caches_pages(self):
        response = self.client.get(reverse("service_worker"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertEqual(response["Cache-Control"], "no-cache")
        body = response.content.decode()
        self.assertIn("request.mode === \"navigate\"", body)
        self.assertNotIn("cache.put(request", body.split("// Pages:")[1])  # page navigations are network-only

    def test_offline_page_contains_no_user_data(self):
        self.client.force_login(self.u_p1)
        response = self.client.get(reverse("offline"))
        self.assertEqual(response.status_code, 200)
        for private in ["Asha", "p1", "Alpha School"]:
            self.assertNotContains(response, private)

    def test_icons_exist(self):
        from django.contrib.staticfiles import finders

        for size in (192, 512):
            self.assertIsNotNone(finders.find(f"web/icons/icon-{size}.png"))
