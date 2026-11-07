import csv
import io
import re
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from academics.models import Enrollment
from accounts.models import Membership, Role, User
from core.models import School
from core.testing import PASSWORD, WorldTestCase
from people.models import Guardian, Staff, Student, StudentGuardian

from . import importing, provisioning
from .importing import ImportFileError
from .models import AccountEvent, ImportBatch
from .provisioning import ProvisionError

SLIP_PASSWORD = re.compile(r"\b[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}\b")

DEFAULT_ROW = {
    "admission_no": "N1001", "first_name": "Ira", "last_name": "Shah", "gender": "Female", "date_of_birth": "2016-05-01",
    "admission_date": "2026-04-01", "class": "Class V", "section": "A", "roll_no": "", "address": "1 Lane",
    "guardian_name": "Raj Shah", "guardian_phone": "9811111111", "guardian_relation": "Father", "guardian_email": "",
    "guardian2_name": "", "guardian2_phone": "", "guardian2_relation": "",
}


def students_csv(*rows, header=None, encoding="utf-8-sig", delimiter=","):
    """A student CSV (as bytes). Each row overrides DEFAULT_ROW; pass a dict, or None for a blank line."""
    header = header or importing.STUDENT_COLUMNS
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter)
    writer.writerow(header)
    for row in rows:
        writer.writerow([] if row is None else [{**DEFAULT_ROW, **row}.get(col, "") for col in header])
    return out.getvalue().encode(encoding)


def staff_csv(*rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(importing.STAFF_COLUMNS)
    default = {"employee_code": "T900", "first_name": "Kiran", "last_name": "Das", "gender": "Male", "phone": "9822222222",
               "email": "kiran@example.com", "designation": "Teacher", "date_joined": "2024-06-15"}
    for row in rows:
        writer.writerow([{**default, **row}[c] for c in importing.STAFF_COLUMNS])
    return out.getvalue().encode("utf-8-sig")


class OnboardingCase(WorldTestCase):
    """Fixture reminder: school A has Class 5 (sections A and B) with Asha, Bimal (5A, rolls 1 and 2) and Chitra (5B).
    Roles: admin (principal), office, accountant, teacher, parent, student. u_office may manage parent/student logins;
    only u_admin may manage staff logins."""

    def setUp(self):
        super().setUp()
        self.nila = Guardian.objects.create(school=self.school_a, first_name="Nila", last_name="Rao", phone="9000000011")
        StudentGuardian.objects.create(school=self.school_a, student=self.dev, guardian=self.nila, relation="mother")
        self.tara = Staff.objects.create(school=self.school_a, employee_code="T9", first_name="Tara", last_name="Nair", phone="9000000013")
        self.newcomer = Student.objects.create(school=self.school_a, admission_no="NEW9", first_name="Neel", last_name="Das")
        self.role = lambda code: Role.objects.get(school=self.school_a, code=code)

    def make_login(self, kind, person, actor=None, **kw):
        return provisioning.create_login(self.school_a, kind, person, actor or self.u_office, **kw)


# =============================================================================================
# Creating logins
# =============================================================================================

class PasswordGenerationTests(OnboardingCase):
    def test_temporary_codes_are_readable_and_random(self):
        codes = {provisioning.generate_password() for _ in range(300)}
        self.assertEqual(len(codes), 300)
        for code in codes:
            self.assertRegex(code, r"^[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}$")
            self.assertNotRegex(code, "[01OIL]")  # nothing that is confusing on a printed slip


class CreateLoginTests(OnboardingCase):
    def test_a_parent_login_uses_their_phone_number(self):
        result = self.make_login("guardian", self.nila)
        user = result.user
        self.assertEqual((user.username, user.phone, user.first_name, user.last_name), ("9000000011", "9000000011", "Nila", "Rao"))
        self.assertTrue(user.check_password(result.password))
        self.assertTrue(user.must_change_password)
        self.assertEqual(result.role.code, "parent")
        self.assertIsNone(result.person.user_id and None)
        self.nila.refresh_from_db()
        self.assertEqual(self.nila.user, user)
        self.assertTrue(Membership.objects.filter(user=user, school=self.school_a, role=self.role("parent"), is_active=True).exists())
        self.assertFalse(result.linked)

    def test_the_password_is_only_ever_stored_hashed(self):
        result = self.make_login("guardian", self.nila)
        self.assertNotIn(result.password, result.user.password)
        self.assertTrue(result.user.password.startswith(("pbkdf2", "md5", "argon2", "bcrypt")))

    def test_the_new_login_really_works_and_leads_to_a_forced_change(self):
        result = self.make_login("guardian", self.nila)
        response = self.client.post(reverse("login"), {"username": "9000000011", "password": result.password}, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("password_change"))

    def test_a_student_login_uses_the_admission_number(self):
        result = self.make_login("student", self.newcomer)
        self.assertEqual((result.user.username, result.role.code), ("new9", "student"))
        self.assertIsNone(result.user.phone)

    def test_usernames_never_collide(self):
        User.objects.create_user(username="new9", password="x")
        self.assertEqual(self.make_login("student", self.newcomer).user.username, "new92")

    def test_staff_logins_need_a_staff_role_and_the_principal(self):
        with self.assertRaisesMessage(ProvisionError, "don't have permission"):
            self.make_login("staff", self.tara, actor=self.u_office, role_id=self.role("teacher").pk)
        with self.assertRaisesMessage(ProvisionError, "Choose a role"):
            self.make_login("staff", self.tara, actor=self.u_admin)
        with self.assertRaisesMessage(ProvisionError, "staff role"):
            self.make_login("staff", self.tara, actor=self.u_admin, role_id=self.role("parent").pk)
        result = self.make_login("staff", self.tara, actor=self.u_admin, role_id=self.role("teacher").pk)
        self.assertEqual((result.user.username, result.user.phone, result.role.code), ("t9", "9000000013", "teacher"))

    def test_only_roles_of_this_school_can_be_given(self):
        with self.assertRaises(ProvisionError):
            self.make_login("staff", self.tara, actor=self.u_admin, role_id=self.roles_b["teacher"].pk)

    def test_who_may_create_which_logins(self):
        for actor in (self.u_t1, self.u_p1, self.u_student, self.u_accountant):
            with self.assertRaisesMessage(ProvisionError, "don't have permission", msg=actor.username):
                self.make_login("guardian", self.nila, actor=actor)
        self.make_login("guardian", self.nila, actor=self.u_office)  # office staff: yes, for parents and students

    def test_parents_need_a_valid_phone_number(self):
        for phone in ("", "12345", "abc"):
            self.nila.phone = phone
            self.nila.save()
            with self.assertRaisesMessage(ProvisionError, "10-digit phone number", msg=phone):
                self.make_login("guardian", self.nila)

    def test_someone_who_already_has_a_login_is_refused(self):
        with self.assertRaisesMessage(ProvisionError, "already has a login"):
            self.make_login("guardian", Guardian.objects.get(user=self.u_p1))

    def test_a_person_from_another_school_is_refused(self):
        other = Guardian.objects.create(school=self.school_b, first_name="Zed", phone="9000000099")
        with self.assertRaisesMessage(ProvisionError, "doesn't belong to this school"):
            self.make_login("guardian", other)

    def test_a_school_without_the_parent_role_gets_a_clear_message(self):
        Role.objects.filter(school=self.school_a, code="parent").update(code="guardian-role")
        with self.assertRaisesMessage(ProvisionError, "no 'parent' role"):
            self.make_login("guardian", self.nila)

    def test_an_existing_login_with_the_same_phone_is_reused_and_left_untouched(self):
        # A teacher whose number is also on a parent record signs in once: the parent role is added to the same login.
        self.u_t1.phone = "9000000011"
        self.u_t1.save()
        before = self.u_t1.password
        result = self.make_login("guardian", self.nila)
        self.assertTrue(result.linked)
        self.assertIsNone(result.password)  # nothing to hand out: their own password still works
        self.assertEqual(result.user, self.u_t1)
        self.u_t1.refresh_from_db()
        self.assertEqual((self.u_t1.password, self.u_t1.must_change_password), (before, False))
        self.assertEqual({m.role.code for m in Membership.objects.filter(user=self.u_t1, school=self.school_a)}, {"teacher", "parent"})
        self.assertEqual(AccountEvent.objects.get().action, "linked")

    def test_a_deactivated_or_platform_account_is_never_silently_reused(self):
        self.u_t1.phone = "9000000011"
        self.u_t1.is_active = False
        self.u_t1.save()
        with self.assertRaisesMessage(ProvisionError, "deactivated login"):
            self.make_login("guardian", self.nila)
        self.u_t1.is_active, self.u_t1.is_superuser = True, True
        self.u_t1.save()
        with self.assertRaisesMessage(ProvisionError, "platform administrator"):
            self.make_login("guardian", self.nila)

    def test_two_records_sharing_a_phone_do_not_collide(self):
        twin = Guardian.objects.create(school=self.school_a, first_name="Twin", phone="9000000011")
        StudentGuardian.objects.create(school=self.school_a, student=self.dev, guardian=twin)
        self.make_login("guardian", self.nila)
        with self.assertRaisesMessage(ProvisionError, "already linked to Nila Rao"):
            self.make_login("guardian", twin)

    def test_every_creation_is_logged(self):
        self.make_login("guardian", self.nila)
        event = AccountEvent.objects.get()
        self.assertEqual((event.action, event.actor, event.target_label, event.school), ("created", self.u_office, "Nila Rao", self.school_a))
        self.assertIn("Parent / Guardian", event.detail)


class BulkCreateTests(OnboardingCase):
    def test_creates_many_and_reports_who_was_skipped(self):
        no_phone = Guardian.objects.create(school=self.school_a, first_name="Mute", phone="")
        StudentGuardian.objects.create(school=self.school_a, student=self.dev, guardian=no_phone)
        results, skipped = provisioning.bulk_create_logins(self.school_a, "guardian", self.u_office, [self.nila, no_phone])
        self.assertEqual([r.person for r in results], [self.nila])
        self.assertEqual([(p.first_name, "10-digit" in why) for p, why in skipped], [("Mute", True)])

    def test_a_shared_parent_gets_one_login_for_all_their_children(self):
        StudentGuardian.objects.create(school=self.school_a, student=self.chitra, guardian=self.nila)
        results, _ = provisioning.bulk_create_logins(self.school_a, "guardian", self.u_office, [self.nila])
        self.assertEqual(len(results), 1)
        self.assertEqual(Membership.objects.filter(user=results[0].user).count(), 1)

    def test_without_permission_nothing_is_created(self):
        results, skipped = provisioning.bulk_create_logins(self.school_a, "guardian", self.u_t1, [self.nila])
        self.assertEqual((results, len(skipped)), ([], 1))
        self.assertFalse(User.objects.filter(phone="9000000011").exists())


# =============================================================================================
# Managing logins: reset, deactivate
# =============================================================================================

class ManageAccountRulesTests(OnboardingCase):
    def can(self, actor, target):
        return provisioning.can_manage_account(actor, self.school_a, target)

    def test_office_manages_parents_and_students_only(self):
        self.assertTrue(self.can(self.u_office, self.u_p1)[0])
        self.assertTrue(self.can(self.u_office, self.u_student)[0])
        for staff_login in (self.u_t1, self.u_admin, self.u_accountant, self.u_office):
            self.assertFalse(self.can(self.u_office, staff_login)[0], staff_login.username)

    def test_a_teacher_who_is_also_a_parent_is_a_staff_login(self):
        ok, why = self.can(self.u_office, self.u_dual)  # teacher + parent: resetting it would hand over the teacher access
        self.assertFalse(ok)
        self.assertIn("principal", why)
        self.assertTrue(self.can(self.u_admin, self.u_dual)[0])

    def test_the_principal_manages_every_login_in_the_school(self):
        for target in (self.u_t1, self.u_p1, self.u_student, self.u_accountant, self.u_office):
            self.assertTrue(self.can(self.u_admin, target)[0], target.username)

    def test_nobody_manages_themselves_a_platform_owner_or_another_schools_login(self):
        self.assertIn("own account", self.can(self.u_admin, self.u_admin)[1])
        owner = get_user_model().objects.create_superuser("owner", password=PASSWORD)
        Membership.objects.create(user=owner, role=self.role("parent"))
        self.assertIn("platform administrator", self.can(self.u_admin, owner)[1])
        self.assertIn("another school", self.can(self.u_admin, self.u_pb)[1]) if Membership.objects.filter(user=self.u_pb).exclude(school=self.school_a).exists() else None
        self.assertFalse(self.can(self.u_admin, self.u_pb)[0])  # a school B parent: no role here at all

    def test_a_login_shared_with_another_school_is_off_limits(self):
        Membership.objects.create(user=self.u_p1, role=self.roles_b["parent"])
        ok, why = self.can(self.u_admin, self.u_p1)
        self.assertFalse(ok)
        self.assertIn("another school", why)

    def test_people_without_any_role_here_cannot_be_managed(self):
        ok, why = self.can(self.u_admin, self.u_orphan)
        self.assertFalse(ok)
        self.assertIn("no role", why)


class ResetAndDeactivateTests(OnboardingCase):
    def test_reset_gives_a_new_temporary_password(self):
        password = provisioning.reset_password(self.school_a, self.u_p1, self.u_office)
        self.u_p1.refresh_from_db()
        self.assertTrue(self.u_p1.check_password(password))
        self.assertFalse(self.u_p1.check_password(PASSWORD))  # the old one is gone
        self.assertTrue(self.u_p1.must_change_password)
        event = AccountEvent.objects.get()
        self.assertEqual((event.action, event.actor, event.target), ("reset", self.u_office, self.u_p1))
        self.assertNotIn(password, event.detail)  # the log never holds the password

    def test_reset_ends_a_signed_in_session(self):
        self.client.force_login(self.u_p1)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        provisioning.reset_password(self.school_a, self.u_p1, self.u_office)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)  # Django signs out the old session

    def test_office_cannot_reset_staff_and_the_account_stays_untouched(self):
        before = self.u_t1.password
        with self.assertRaisesMessage(ProvisionError, "principal"):
            provisioning.reset_password(self.school_a, self.u_t1, self.u_office)
        self.u_t1.refresh_from_db()
        self.assertEqual(self.u_t1.password, before)
        self.assertFalse(AccountEvent.objects.exists())

    def test_resetting_a_deactivated_login_asks_to_reactivate_first(self):
        provisioning.set_active(self.school_a, self.u_p1, self.u_office, False)
        with self.assertRaisesMessage(ProvisionError, "Reactivate it first"):
            provisioning.reset_password(self.school_a, self.u_p1, self.u_office)

    def test_deactivating_and_reactivating(self):
        self.client.force_login(self.u_p1)
        provisioning.set_active(self.school_a, self.u_p1, self.u_office, False)
        self.u_p1.refresh_from_db()
        self.assertFalse(self.u_p1.is_active)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)  # signed out at once
        self.assertContains(self.client.post(reverse("login"), {"username": "p1", "password": PASSWORD}), "Please enter a correct")
        provisioning.set_active(self.school_a, self.u_p1, self.u_office, True)
        self.assertEqual(self.client.post(reverse("login"), {"username": "p1", "password": PASSWORD}).status_code, 302)
        self.assertEqual([e.action for e in AccountEvent.objects.order_by("pk")], ["deactivated", "activated"])

    def test_repeating_an_action_changes_nothing_and_logs_nothing(self):
        provisioning.set_active(self.school_a, self.u_p1, self.u_office, True)  # already active
        self.assertFalse(AccountEvent.objects.exists())

    def test_office_cannot_deactivate_staff(self):
        with self.assertRaises(ProvisionError):
            provisioning.set_active(self.school_a, self.u_t1, self.u_office, False)
        self.u_t1.refresh_from_db()
        self.assertTrue(self.u_t1.is_active)

    def test_login_status_labels(self):
        guardian = Guardian.objects.select_related("user").get(user=self.u_p1)
        self.assertEqual(provisioning.login_status(guardian), ("active", "Active"))
        guardian.user.must_change_password = True
        self.assertEqual(provisioning.login_status(guardian)[0], "must_change")
        guardian.user.is_active = False
        self.assertEqual(provisioning.login_status(guardian)[0], "inactive")
        self.assertEqual(provisioning.login_status(self.nila), ("none", "No login"))

    def test_hub_counts(self):
        counts = provisioning.hub_counts(self.school_a)
        self.assertEqual(counts["guardians_without_login"], 1)  # Nila
        self.assertEqual(counts["staff_without_login"], 1)       # Tara
        self.assertEqual(counts["students_without_login"], 4)    # all but Asha, plus the newcomer
        self.assertEqual((counts["temporary_passwords"], counts["deactivated"]), (0, 0))
        provisioning.reset_password(self.school_a, self.u_p1, self.u_office)
        provisioning.set_active(self.school_a, self.u_p2, self.u_office, False)
        counts = provisioning.hub_counts(self.school_a)
        self.assertEqual((counts["temporary_passwords"], counts["deactivated"]), (1, 1))


# =============================================================================================
# Reading and checking files
# =============================================================================================

class HelperTests(OnboardingCase):
    def test_class_names_are_matched_loosely(self):
        for text in ("Class V", "class 5", "5", "5th", "std 5", "Standard V", "V", "Grade 5"):
            self.assertEqual(importing.class_key(text), "5", text)
        self.assertEqual(importing.class_key("Class X"), "10")
        self.assertEqual(importing.class_key("UKG"), "ukg")
        self.assertEqual(importing.class_key("Pre Nursery"), "prenursery")

    def test_dates_are_read_day_first(self):
        for text in ("2015-04-23", "23/04/2015", "23-04-2015", "23.04.2015", "23-Apr-2015", "23 April 2015"):
            self.assertEqual(importing.parse_date(text), date(2015, 4, 23), text)
        self.assertEqual(importing.parse_date("03/04/2015"), date(2015, 4, 3))  # 3 April, not March 4th
        with self.assertRaises(ValueError):
            importing.parse_date("April-ish")

    def test_names_are_split_at_the_last_space(self):
        self.assertEqual(importing.split_name("Ravi Kumar Verma"), ("Ravi Kumar", "Verma"))
        self.assertEqual(importing.split_name("  Sunita "), ("Sunita", ""))
        self.assertEqual(importing.split_name(""), ("", ""))

    def test_formula_like_text_is_defused_in_downloads(self):
        self.assertEqual(importing.csv_safe("=SUM(1+1)"), "'=SUM(1+1)")
        self.assertEqual(importing.csv_safe("+91 98"), "'+91 98")
        self.assertEqual(importing.csv_safe("Asha"), "Asha")
        self.assertEqual(importing.csv_safe(None), "")


class ReadCsvTests(OnboardingCase):
    def read(self, raw):
        return importing.read_csv(raw, importing.STUDENT_COLUMNS, importing.STUDENT_REQUIRED, importing.STUDENT_SYNONYMS)

    def test_excel_style_files_work(self):
        for kwargs in ({"encoding": "utf-8-sig"}, {"encoding": "utf-8"}, {"delimiter": ";"}, {"delimiter": "\t"}):
            records, _ = self.read(students_csv({}, **kwargs))
            self.assertEqual(records[0]["values"]["admission_no"], "N1001", kwargs)

    def test_old_excel_encoding_and_hindi_names(self):
        raw = students_csv({"first_name": "Zoë"}, encoding="cp1252")
        self.assertEqual(self.read(raw)[0][0]["values"]["first_name"], "Zoë")
        records, _ = self.read(students_csv({"first_name": "आरव"}))
        self.assertEqual(records[0]["values"]["first_name"], "आरव")

    def test_file_that_is_not_text_is_refused(self):
        with self.assertRaisesMessage(ImportFileError, "CSV UTF-8"):
            self.read(b"\x81\x8d\x8f\x90\x9d")

    def test_empty_and_header_only_files(self):
        for raw in (b"", b"\n\n", b"   ,  ,"):
            with self.assertRaisesMessage(ImportFileError, "empty"):
                self.read(raw)
        self.assertEqual(self.read(students_csv())[0], [])  # headings only: valid, just no rows

    def test_missing_required_columns_are_named(self):
        raw = students_csv({}, header=["admission_no", "first_name", "last_name"])
        with self.assertRaisesMessage(ImportFileError, "class, section"):
            self.read(raw)

    def test_everyday_headings_are_understood_and_strangers_are_reported(self):
        header = ["Adm No", "First Name", "Surname", "DOB", "Standard", "Sec", "Roll", "Parent Name", "Mobile", "Favourite Colour"]
        raw = students_csv({"admission_no": "X1"}, header=["admission_no", "first_name", "last_name", "date_of_birth", "class", "section", "roll_no", "guardian_name", "guardian_phone"])
        raw = raw.replace(b"admission_no,first_name,last_name,date_of_birth,class,section,roll_no,guardian_name,guardian_phone", ",".join(header).encode())
        records, ignored = self.read(raw)
        self.assertEqual(ignored, ["Favourite Colour"])
        v = records[0]["values"]
        self.assertEqual((v["admission_no"], v["last_name"], v["class"], v["section"], v["guardian_phone"]), ("X1", "Shah", "Class V", "A", "9811111111"))

    def test_blank_lines_are_skipped_and_row_numbers_match_the_spreadsheet(self):
        records, _ = self.read(students_csv({"admission_no": "A"}, None, {"admission_no": "B"}))
        self.assertEqual([(r["row"], r["values"]["admission_no"]) for r in records], [(2, "A"), (4, "B")])

    def test_size_limits(self):
        with self.assertRaisesMessage(ImportFileError, "2 MB"):
            self.read(b"x" * (importing.MAX_BYTES + 1))
        with patch.object(importing, "MAX_ROWS", 3):
            with self.assertRaisesMessage(ImportFileError, "more than 3 rows"):
                self.read(students_csv(*[{"admission_no": f"R{i}"} for i in range(4)]))


class CheckStudentsTests(OnboardingCase):
    def check(self, *rows):
        records, _ = importing.read_csv(students_csv(*rows), importing.STUDENT_COLUMNS, importing.STUDENT_REQUIRED, importing.STUDENT_SYNONYMS)
        return importing.check_students(self.school_a, records)

    def errors(self, row):
        good, problems, skipped, _ = self.check(row)
        return [e for p in problems for e in p["errors"]] if problems else []

    def test_a_good_row(self):
        good, problems, skipped, summary = self.check({})
        self.assertEqual((len(good), problems, skipped), (1, [], []))
        row = good[0]
        self.assertEqual((row["admission_no"], row["first_name"], row["gender"], row["date_of_birth"], row["section_label"]), ("N1001", "Ira", "F", "2016-05-01", "Class 5-A"))
        self.assertEqual(row["guardians"], [{"name": "Raj Shah", "phone": "9811111111", "email": "", "relation": "father", "primary": True}])
        self.assertEqual((summary["new_students"], summary["new_parents"], summary["existing_parents_linked"]), (1, 1, 0))

    def test_existing_students_are_skipped_not_overwritten_and_not_an_error(self):
        good, problems, skipped, summary = self.check({"admission_no": "A1"}, {"admission_no": "a2"}, {"admission_no": "FRESH"})
        self.assertEqual([r["admission_no"] for r in good], ["FRESH"])
        self.assertEqual(problems, [])
        self.assertEqual([s["admission_no"] for s in skipped], ["A1", "a2"])  # case-insensitive match
        self.assertEqual(summary["skipped_existing"], 2)

    def test_the_same_admission_number_twice_in_one_file(self):
        good, problems, _, _ = self.check({"admission_no": "D1"}, {"admission_no": "d1", "first_name": "Other"})
        self.assertEqual(len(good), 1)
        self.assertIn("repeated (also on row 2)", problems[0]["errors"][0])
        self.assertEqual(problems[0]["row"], 3)

    def test_required_things(self):
        self.assertIn("Admission number is required.", self.errors({"admission_no": ""}))
        self.assertIn("First name is required.", self.errors({"first_name": ""}))
        self.assertIn("Class is required.", self.errors({"class": ""}))
        self.assertIn("Section is required.", self.errors({"section": ""}))

    def test_unknown_class_lists_the_real_ones(self):
        message = self.errors({"class": "Class XI"})[0]
        self.assertIn("Unknown class 'Class XI'", message)
        self.assertIn("Class 5", message)

    def test_unknown_section_lists_the_real_ones(self):
        message = self.errors({"section": "Z"})[0]
        self.assertIn("Class 5 has no section 'Z'", message)
        self.assertIn("A, B", message)

    def test_sections_are_matched_regardless_of_case(self):
        self.assertEqual(self.check({"section": "b"})[0][0]["section_label"], "Class 5-B")

    def test_dates_gender_and_lengths(self):
        self.assertTrue(any("Date of birth" in e for e in self.errors({"date_of_birth": "someday"})))
        self.assertTrue(any("looks wrong" in e for e in self.errors({"date_of_birth": "1950-01-01"})))
        self.assertTrue(any("looks wrong" in e for e in self.errors({"date_of_birth": "2030-01-01"})))
        self.assertTrue(any("Gender" in e for e in self.errors({"gender": "robot"})))
        self.assertTrue(any("too long" in e for e in self.errors({"first_name": "x" * 81})))
        self.assertTrue(any("Admission date" in e for e in self.errors({"admission_date": "31/02/2026"})))
        for text, code in (("Boy", "M"), ("girl", "F"), ("O", "O"), ("", "")):
            self.assertEqual(self.check({"gender": text})[0][0]["gender"], code, text)

    def test_roll_numbers(self):
        self.assertTrue(any("already used in Class 5-A" in e for e in self.errors({"roll_no": "1"})))  # Asha has roll 1
        self.assertTrue(any("whole number" in e for e in self.errors({"roll_no": "abc"})))
        good, problems, _, _ = self.check({"admission_no": "R1", "roll_no": "9"}, {"admission_no": "R2", "roll_no": "9"})
        self.assertEqual(len(good), 1)
        self.assertIn("repeated in Class 5-A (also on row 2)", problems[0]["errors"][0])
        self.assertEqual(self.check({"roll_no": "9"})[0][0]["roll_no"], 9)

    def test_parents(self):
        self.assertTrue(any("10-digit" in e for e in self.errors({"guardian_phone": "98765"})))
        self.assertTrue(any("name is missing" in e for e in self.errors({"guardian_name": ""})))
        self.assertTrue(any("doesn't look right" in e for e in self.errors({"guardian_email": "not-an-email"})))
        self.assertEqual(self.check({"guardian_phone": "+91 98111-11111"})[0][0]["guardians"][0]["phone"], "9811111111")

    def test_warnings_do_not_block_a_row(self):
        good, problems, _, _ = self.check({"guardian_phone": "5811111111", "guardian_relation": "Uncle"})
        self.assertEqual(problems, [])
        warnings = " ".join(good[0]["warnings"])
        self.assertIn("doesn't look like a mobile", warnings)
        self.assertIn("Relation 'uncle'", warnings)
        good, _, _, summary = self.check({"guardian_name": "", "guardian_phone": ""})
        self.assertIn("No parent or guardian given.", good[0]["warnings"])
        good, _, _, summary = self.check({"guardian_phone": ""})
        self.assertEqual(summary["parents_without_phone"], 1)

    def test_a_second_parent_and_duplicate_phones(self):
        row = {"guardian2_name": "Sia Shah", "guardian2_phone": "9822222222", "guardian2_relation": "Mother"}
        guardians = self.check(row)[0][0]["guardians"]
        self.assertEqual([(g["relation"], g["primary"]) for g in guardians], [("father", True), ("mother", False)])
        same = self.check({"guardian2_name": "Raj Again", "guardian2_phone": "9811111111"})[0][0]
        self.assertEqual(len(same["guardians"]), 1)
        self.assertTrue(any("same phone number" in w for w in same["warnings"]))

    def test_siblings_and_existing_parents_are_recognised_by_phone(self):
        good, _, _, summary = self.check({"admission_no": "S1"}, {"admission_no": "S2", "first_name": "Sib"})
        self.assertEqual(summary["new_parents"], 1)  # one phone, two children
        _, _, _, summary = self.check({"guardian_phone": "9876543210", "guardian_name": "Pia Test"})  # Pia (u_p1) exists already
        self.assertEqual((summary["new_parents"], summary["existing_parents_linked"]), (0, 1))

    def test_the_same_phone_with_a_different_name_is_flagged(self):
        good, _, _, _ = self.check({"admission_no": "P1"}, {"admission_no": "P2", "guardian_name": "Someone Else"})
        self.assertTrue(any("also used by 'Raj Shah'" in w for w in good[1]["warnings"]))

    def test_nothing_is_written_while_checking(self):
        before = (Student.objects.count(), Guardian.objects.count(), Enrollment.objects.count())
        self.check({}, {"admission_no": "N2"})
        self.assertEqual((Student.objects.count(), Guardian.objects.count(), Enrollment.objects.count()), before)

    def test_a_school_without_a_current_year_cannot_import_students(self):
        self.year_a.is_current = False
        self.year_a.save()
        with self.assertRaisesMessage(ImportFileError, "current academic year"):
            self.check({})

    def test_the_downloadable_template_is_itself_valid(self):
        records, _ = importing.read_csv(importing.template_csv("students").encode(), importing.STUDENT_COLUMNS, importing.STUDENT_REQUIRED, importing.STUDENT_SYNONYMS)
        good, problems, _, _ = importing.check_students(self.school_a, records)
        self.assertEqual((len(good), problems), (1, []))
        self.assertEqual(len(good[0]["guardians"]), 2)

    def test_values_that_look_like_formulas_are_just_text(self):
        good, _, _, _ = self.check({"first_name": "=HYPERLINK('x')", "address": "@home"})
        self.assertEqual((good[0]["first_name"], good[0]["address"]), ("=HYPERLINK('x')", "@home"))


# =============================================================================================
# Applying an import
# =============================================================================================

class ApplyStudentsTests(OnboardingCase):
    def stage(self, *rows, user=None):
        return importing.stage_students(self.school_a, user or self.u_office, "students.csv", students_csv(*rows))

    def test_creates_students_enrolments_and_parents(self):
        batch = self.stage({}, {"admission_no": "N1002", "first_name": "Sib", "guardian_name": "Raj Shah", "section": "B"})
        result = importing.apply_students(batch)
        self.assertEqual((result["students"], result["parents_created"], result["parents_linked"], result["skipped"]), (2, 1, 1, 0))
        ira = Student.objects.get(admission_no="N1001")
        self.assertEqual((ira.first_name, ira.gender, ira.date_of_birth, ira.admission_date, ira.status), ("Ira", "F", date(2016, 5, 1), date(2026, 4, 1), "active"))
        enrolment = Enrollment.objects.get(student=ira)
        self.assertEqual((enrolment.section, enrolment.academic_year), (self.sec5a, self.year_a))
        raj = Guardian.objects.get(phone="9811111111")
        self.assertEqual((raj.first_name, raj.last_name, raj.gender, raj.school), ("Raj", "Shah", "M", self.school_a))
        self.assertEqual(set(StudentGuardian.objects.filter(guardian=raj).values_list("student__admission_no", flat=True)), {"N1001", "N1002"})  # siblings share one parent
        link = StudentGuardian.objects.get(guardian=raj, student=ira)
        self.assertEqual((link.relation, link.is_primary), ("father", True))

    def test_nobody_gets_a_login_from_an_import(self):
        importing.apply_students(self.stage({}))
        self.assertFalse(Guardian.objects.get(phone="9811111111").user_id)
        self.assertFalse(Student.objects.get(admission_no="N1001").user_id)
        self.assertFalse(User.objects.filter(phone="9811111111").exists())

    def test_automatic_roll_numbers_continue_after_the_highest_existing(self):
        importing.apply_students(self.stage({"admission_no": "A"}, {"admission_no": "B"}))
        self.assertEqual(list(Enrollment.objects.filter(student__admission_no__in=["A", "B"]).order_by("student__admission_no").values_list("roll_no", flat=True)), [3, 4])

    def test_explicit_roll_numbers_are_kept_and_automatic_ones_avoid_them(self):
        importing.apply_students(self.stage({"admission_no": "A", "roll_no": "3"}, {"admission_no": "B"}))
        rolls = dict(Enrollment.objects.filter(student__admission_no__in=["A", "B"]).values_list("student__admission_no", "roll_no"))
        self.assertEqual(rolls, {"A": 3, "B": 4})

    def test_an_existing_parent_is_reused_not_duplicated(self):
        importing.apply_students(self.stage({"guardian_phone": "9876543210", "guardian_name": "Pia Test"}))
        self.assertEqual(Guardian.objects.filter(phone="9876543210").count(), 1)
        self.assertTrue(StudentGuardian.objects.filter(guardian__user=self.u_p1, student__admission_no="N1001").exists())

    def test_parents_without_a_phone_get_their_own_records(self):
        importing.apply_students(self.stage({"admission_no": "A", "guardian_phone": ""}, {"admission_no": "B", "guardian_phone": ""}))
        self.assertEqual(Guardian.objects.filter(school=self.school_a, phone="", first_name="Raj").count(), 2)  # nothing to match them on

    def test_it_cannot_be_applied_twice(self):
        batch = self.stage({})
        importing.apply_students(batch)
        with self.assertRaisesMessage(ImportFileError, "already been applied"):
            importing.apply_students(batch)
        self.assertEqual(Student.objects.filter(admission_no="N1001").count(), 1)

    def test_records_added_after_the_preview_are_not_overwritten(self):
        batch = self.stage({"admission_no": "LATE"})
        Student.objects.create(school=self.school_a, admission_no="late", first_name="Already")  # someone added it meanwhile
        result = importing.apply_students(batch)
        self.assertEqual((result["students"], result["skipped"]), (0, 1))
        self.assertEqual(Student.objects.get(admission_no="late").first_name, "Already")

    def test_a_section_deleted_after_the_preview_is_skipped_safely(self):
        batch = self.stage({"section": "B", "admission_no": "GONE"}, {"admission_no": "OK"})
        Enrollment.objects.filter(section=self.sec5b).delete()
        self.sec5b.delete()
        result = importing.apply_students(batch)
        self.assertEqual((result["students"], result["skipped"]), (1, 1))

    def test_a_failure_part_way_creates_nothing(self):
        batch = self.stage({"admission_no": "T1"}, {"admission_no": "T2"})
        real_create = Enrollment.objects.create
        calls = []

        def flaky(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real_create(**kwargs)

        with patch.object(Enrollment.objects, "create", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                importing.apply_students(batch)
        self.assertFalse(Student.objects.filter(admission_no__in=["T1", "T2"]).exists())  # all or nothing
        self.assertFalse(Guardian.objects.filter(phone="9811111111").exists())
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.PREVIEW)  # and it can be tried again

    def test_only_the_good_rows_are_imported_when_some_are_bad(self):
        batch = self.stage({"admission_no": "OK1"}, {"admission_no": "BAD", "class": "Class XI"})
        self.assertEqual((batch.summary["new_students"], batch.summary["problems"]), (1, 1))
        importing.apply_students(batch)
        self.assertTrue(Student.objects.filter(admission_no="OK1").exists())
        self.assertFalse(Student.objects.filter(admission_no="BAD").exists())

    def test_the_batch_remembers_what_happened(self):
        batch = self.stage({})
        importing.apply_students(batch)
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.created_by, batch.school), ("applied", self.u_office, self.school_a))
        self.assertIsNotNone(batch.applied_at)
        self.assertEqual(batch.result["students"], 1)
        self.assertEqual(len(batch.result["student_ids"]), 1)


class StaffImportTests(OnboardingCase):
    def stage(self, *rows):
        return importing.stage_staff(self.school_a, self.u_admin, "staff.csv", staff_csv(*rows))

    def test_check_and_apply(self):
        batch = self.stage({}, {"employee_code": "T901", "first_name": "Lata", "phone": "", "email": "", "date_joined": ""})
        self.assertEqual((batch.summary["new_staff"], batch.summary["problems"]), (2, 0))
        result = importing.apply_staff(batch)
        self.assertEqual(result, {"staff": 2, "skipped": 0})
        kiran = Staff.objects.get(employee_code="T900")
        self.assertEqual((kiran.first_name, kiran.phone, kiran.email, kiran.designation, kiran.date_joined, kiran.gender, kiran.user_id), ("Kiran", "9822222222", "kiran@example.com", "Teacher", date(2024, 6, 15), "M", None))

    def test_existing_codes_are_skipped_and_bad_rows_reported(self):
        batch = self.stage({"employee_code": "t1"}, {"employee_code": "T902", "email": "nope"}, {"employee_code": "T903", "phone": "12"},
                           {"employee_code": "T904", "date_joined": "2099-01-01"}, {"employee_code": "T900"}, {"employee_code": "t900"})
        errors = {p["row"]: p["errors"] for p in batch.problems}
        self.assertTrue(any("Email" in e for e in errors[3]))
        self.assertTrue(any("10-digit" in e for e in errors[4]))
        self.assertTrue(any("Date joined" in e for e in errors[5]))
        self.assertTrue(any("repeated" in e for e in errors[7]))
        self.assertEqual(batch.summary["skipped_existing"], 1)  # "t1" matches the fixture teacher T1, whatever the case
        self.assertEqual(batch.summary["new_staff"], 1)          # only the first T900 is good; the repeat is a problem

    def test_apply_twice_is_refused(self):
        batch = self.stage({})
        importing.apply_staff(batch)
        with self.assertRaises(ImportFileError):
            importing.apply_staff(batch)


class ProblemsCsvTests(OnboardingCase):
    def test_only_failed_rows_come_back_sanitised(self):
        batch = importing.stage_students(
            self.school_a, self.u_office, "s.csv",
            students_csv({"admission_no": "OK"}, {"admission_no": "=BAD", "first_name": "=cmd()", "class": "Class XI"}),
        )
        rows = list(csv.reader(io.StringIO(importing.problems_csv(batch))))
        self.assertEqual(rows[0][:3], ["problems", "admission_no", "first_name"])
        self.assertEqual(len(rows), 2)  # header + the one failed row
        self.assertIn("Unknown class", rows[1][0])
        self.assertEqual(rows[1][1], "'=BAD")  # not a live formula
        self.assertEqual(rows[1][2], "'=cmd()")


# =============================================================================================
# Screens and access
# =============================================================================================

class SetupScreenCase(OnboardingCase):
    def get(self, user, name, *args, **params):
        self.client.force_login(user)
        return self.client.get(reverse(name, args=args), params)

    def post(self, user, name, *args, data=None, **kw):
        self.client.force_login(user)
        return self.client.post(reverse(name, args=args), data or {}, **kw)

    def status(self, user, name, *args):
        return self.get(user, name, *args).status_code

    def upload(self, user, content, kind="students", name="people.csv", follow=False):
        self.client.force_login(user)
        return self.client.post(reverse("setup_import"), {"kind": kind, "file": SimpleUploadedFile(name, content, content_type="text/csv")}, follow=follow)


class AccessTests(SetupScreenCase):
    def test_everything_needs_a_login(self):
        urls = [reverse("setup_home"), reverse("setup_import"), reverse("setup_people", args=["guardians"]), reverse("setup_bulk", args=["guardians"]),
                reverse("setup_import_template", args=["students"])]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn(reverse("login"), response["Location"])

    def test_only_the_office_and_principal_get_in(self):
        for user in (self.u_admin, self.u_office):
            self.assertEqual(self.status(user, "setup_home"), 200, user.username)
        for user in (self.u_accountant, self.u_t1, self.u_p1, self.u_student, self.u_orphan):  # the orphan has no school role at all
            self.assertEqual(self.status(user, "setup_home"), 403, user.username)

    def test_staff_pages_are_for_the_principal_only(self):
        for name in ("setup_people", "setup_bulk"):
            self.assertEqual(self.status(self.u_admin, name, "staff"), 200)
            self.assertEqual(self.status(self.u_office, name, "staff"), 403)
            self.assertEqual(self.status(self.u_office, name, "guardians"), 200)
            self.assertEqual(self.status(self.u_office, name, "students"), 200)
            self.assertEqual(self.status(self.u_admin, name, "nonsense"), 404)

    def test_the_navigation_shows_the_tab_to_the_right_people(self):
        for user, expected in ((self.u_admin, True), (self.u_office, True), (self.u_t1, False), (self.u_p1, False), (self.u_accountant, False)):
            self.client.force_login(user)
            html = self.client.get(reverse("dashboard")).content.decode()
            self.assertEqual("People &amp; logins" in html or "People & logins" in html, expected, user.username)

    def test_the_hub_shows_only_what_you_may_do(self):
        office = self.get(self.u_office, "setup_home")
        self.assertContains(office, "Parents without a login")
        self.assertNotContains(office, "Staff without a login")
        admin = self.get(self.u_admin, "setup_home")
        self.assertContains(admin, "Staff without a login")
        self.assertEqual(admin.context["counts"]["staff_without_login"], 1)


class PeopleScreenTests(SetupScreenCase):
    def test_guardians_list_with_children_and_status(self):
        response = self.get(self.u_office, "setup_people", "guardians")
        rows = {r["person"].first_name: r for r in response.context["rows"]}
        self.assertEqual(rows["Pia"]["status"], "active")
        self.assertEqual(rows["Pia"]["children"], "Asha Test, Chitra Test")
        self.assertEqual(rows["Nila"]["status"], "none")
        self.assertTrue(rows["Pia"]["can_manage"])
        self.assertFalse(rows["Dual"]["can_manage"])  # a teacher's login: not the office's to reset
        self.assertContains(response, "Managed by the principal")

    def test_search_and_status_filters(self):
        names = lambda **p: {r["person"].first_name for r in self.get(self.u_office, "setup_people", "guardians", **p).context["rows"]}
        self.assertEqual(names(q="nil"), {"Nila"})
        self.assertEqual(names(q="9876543210"), {"Pia"})
        self.assertEqual(names(status="none"), {"Nila"})
        self.assertEqual(names(status="active"), {"Pia", "Paul", "Dual"})
        provisioning.reset_password(self.school_a, self.u_p1, self.u_office)
        provisioning.set_active(self.school_a, self.u_p2, self.u_office, False)
        self.assertEqual(names(status="must_change"), {"Pia"})
        self.assertEqual(names(status="inactive"), {"Paul"})

    def test_students_and_staff_lists(self):
        students = self.get(self.u_office, "setup_people", "students")
        self.assertEqual({r["person"].first_name for r in students.context["rows"]}, {"Asha", "Bimal", "Chitra", "Dev", "Neel"})
        staff = self.get(self.u_admin, "setup_people", "staff")
        rows = {r["person"].first_name: r for r in staff.context["rows"]}
        self.assertEqual(rows["Tara"]["status"], "none")
        self.assertEqual(rows["Tara"]["roles"], [])
        self.assertEqual(rows["Tej"]["roles"], ["Teacher"])
        self.assertEqual({r.name for r in staff.context["staff_roles"]}, {"Principal / Admin", "Office staff", "Accountant", "Teacher"})  # staff roles only

    def test_other_schools_people_never_appear(self):
        for word in ("guardians", "students"):
            response = self.get(self.u_office, "setup_people", word)
            self.assertNotIn("Zoya", [r["person"].first_name for r in response.context["rows"]])
            self.assertNotIn("Pb", [r["person"].first_name for r in response.context["rows"]])

    def test_pages_of_forty(self):
        for i in range(45):
            Guardian.objects.create(school=self.school_a, first_name=f"Extra{i:02d}", phone=f"90000{i:05d}")
        response = self.get(self.u_office, "setup_people", "guardians")
        self.assertEqual((len(response.context["rows"]), response.context["page"].paginator.num_pages), (40, 2))
        self.assertEqual(len(self.get(self.u_office, "setup_people", "guardians", page=2).context["rows"]), 9)  # 45 + the 4 in the fixture = 49

    def test_query_count_does_not_grow_with_the_number_of_people(self):
        self.client.force_login(self.u_office)
        url = reverse("setup_people", args=["guardians"])
        self.client.get(url)

        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(ctx)

        before = queries()
        for i in range(15):
            g = Guardian.objects.create(school=self.school_a, first_name=f"Q{i}", phone=f"91000{i:05d}")
            StudentGuardian.objects.create(school=self.school_a, student=self.dev, guardian=g)
        self.assertEqual(queries(), before)


class CreateLoginScreenTests(SetupScreenCase):
    def test_creating_a_login_shows_the_slip_once(self):
        response = self.post(self.u_office, "setup_create_login", "guardians", self.nila.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        html = response.content.decode()
        password = SLIP_PASSWORD.search(html).group(0)
        for text in ("Nila Rao", "9000000011", "Alpha School", "Parent / Guardian", "Parent of Dev Test", "shown only once"):
            self.assertContains(response, text)
        self.assertTrue(User.objects.get(phone="9000000011").check_password(password))
        # Looking at the list again never shows it, and trying again says the person already has a login.
        people = self.get(self.u_office, "setup_people", "guardians")
        self.assertNotContains(people, password)
        again = self.post(self.u_office, "setup_create_login", "guardians", self.nila.pk, follow=True)
        self.assertContains(again, "already has a login")
        self.assertIsNone(SLIP_PASSWORD.search(again.content.decode()))

    def test_staff_logins_are_the_principals_and_need_a_role(self):
        self.assertEqual(self.post(self.u_office, "setup_create_login", "staff", self.tara.pk, data={"role": self.role("teacher").pk}).status_code, 403)
        none = self.post(self.u_admin, "setup_create_login", "staff", self.tara.pk, follow=True)
        self.assertContains(none, "Choose a role")
        done = self.post(self.u_admin, "setup_create_login", "staff", self.tara.pk, data={"role": self.role("teacher").pk})
        self.assertContains(done, "Tara Nair")
        self.assertContains(done, "t9")
        self.assertEqual(Staff.objects.get(pk=self.tara.pk).user.username, "t9")

    def test_other_schools_people_do_not_exist(self):
        other = Guardian.objects.create(school=self.school_b, first_name="Zed", phone="9000000098")
        self.assertEqual(self.post(self.u_office, "setup_create_login", "guardians", other.pk).status_code, 404)

    def test_the_url_refuses_get(self):
        self.assertEqual(self.status(self.u_office, "setup_create_login", "guardians", self.nila.pk), 405)

    def test_a_parent_without_a_phone_gets_a_message_not_a_crash(self):
        self.nila.phone = ""
        self.nila.save()
        response = self.post(self.u_office, "setup_create_login", "guardians", self.nila.pk, follow=True)
        self.assertContains(response, "10-digit phone number")


class AccountActionScreenTests(SetupScreenCase):
    def test_reset_shows_a_new_password_once(self):
        response = self.post(self.u_office, "setup_account_action", self.u_p1.pk, "reset")
        self.assertEqual(response["Cache-Control"], "no-store")
        password = SLIP_PASSWORD.search(response.content.decode()).group(0)
        self.u_p1.refresh_from_db()
        self.assertTrue(self.u_p1.check_password(password))
        self.assertContains(response, "9876543210")
        self.assertContains(response, "New password for")

    def test_office_cannot_reset_a_teacher(self):
        response = self.post(self.u_office, "setup_account_action", self.u_t1.pk, "reset", follow=True)
        self.assertContains(response, "Only the principal can manage staff logins")
        self.assertIsNone(SLIP_PASSWORD.search(response.content.decode()))
        self.u_t1.refresh_from_db()
        self.assertTrue(self.u_t1.check_password(PASSWORD))
        self.assertEqual(self.post(self.u_admin, "setup_account_action", self.u_t1.pk, "reset").status_code, 200)  # the principal can

    def test_accounts_of_other_schools_or_nobody_do_not_exist(self):
        self.assertEqual(self.post(self.u_office, "setup_account_action", self.u_pb.pk, "reset").status_code, 404)
        self.assertEqual(self.post(self.u_admin, "setup_account_action", self.u_orphan.pk, "reset").status_code, 404)
        self.assertEqual(self.post(self.u_admin, "setup_account_action", 99999, "reset").status_code, 404)
        self.u_pb.refresh_from_db()
        self.assertTrue(self.u_pb.check_password(PASSWORD))

    def test_deactivate_and_reactivate_return_to_where_you_were(self):
        back = reverse("setup_people", args=["guardians"]) + "?status=active"
        response = self.post(self.u_office, "setup_account_action", self.u_p2.pk, "deactivate", data={"next": back}, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], back)
        self.assertContains(response, "can no longer sign in")
        self.u_p2.refresh_from_db()
        self.assertFalse(self.u_p2.is_active)
        response = self.post(self.u_office, "setup_account_action", self.u_p2.pk, "activate", follow=True)
        self.assertContains(response, "can sign in again")
        self.u_p2.refresh_from_db()
        self.assertTrue(self.u_p2.is_active)

    def test_a_foreign_next_address_is_ignored(self):
        response = self.post(self.u_office, "setup_account_action", self.u_p2.pk, "deactivate", data={"next": "https://evil.example/steal"})
        self.assertEqual(response["Location"], reverse("setup_home"))

    def test_unknown_actions_and_get_requests(self):
        self.assertEqual(self.post(self.u_office, "setup_account_action", self.u_p1.pk, "explode").status_code, 404)
        self.assertEqual(self.status(self.u_office, "setup_account_action", self.u_p1.pk, "reset"), 405)

    def test_roles_without_rights_are_refused(self):
        for user in (self.u_t1, self.u_p1, self.u_student, self.u_accountant):
            self.assertEqual(self.post(user, "setup_account_action", self.u_p2.pk, "reset").status_code, 403, user.username)
        self.u_p2.refresh_from_db()
        self.assertTrue(self.u_p2.check_password(PASSWORD))


class BulkScreenTests(SetupScreenCase):
    def test_preview_then_create_for_everyone_without_a_login(self):
        page = self.get(self.u_office, "setup_bulk", "guardians")
        self.assertEqual(page.context["count"], 1)
        response = self.post(self.u_office, "setup_bulk", "guardians")
        self.assertContains(response, "Nila Rao")
        self.assertEqual(len(SLIP_PASSWORD.findall(response.content.decode())), 1)
        self.assertEqual(self.get(self.u_office, "setup_bulk", "guardians").context["count"], 0)
        self.assertContains(self.get(self.u_office, "setup_bulk", "guardians"), "Everyone already has a login")

    def test_parents_without_a_phone_are_counted_and_listed_as_skipped(self):
        mute = Guardian.objects.create(school=self.school_a, first_name="Mute", phone="")
        StudentGuardian.objects.create(school=self.school_a, student=self.dev, guardian=mute)
        self.assertEqual(self.get(self.u_office, "setup_bulk", "guardians").context["no_phone"], 1)
        response = self.post(self.u_office, "setup_bulk", "guardians")
        self.assertContains(response, "Not created (1)")
        self.assertContains(response, "Mute")

    def test_students_can_be_limited_to_one_class(self):
        page = self.get(self.u_office, "setup_bulk", "students", grade=self.grade6.pk)
        self.assertEqual(page.context["count"], 1)  # only Dev is in Class 6 without a login
        response = self.post(self.u_office, "setup_bulk", "students", data={"grade": self.grade6.pk})
        self.assertContains(response, "Dev Test")
        self.assertTrue(Student.objects.get(pk=self.dev.pk).user_id)
        self.assertFalse(Student.objects.get(pk=self.bimal.pk).user_id)

    def test_staff_bulk_needs_a_role(self):
        refused = self.post(self.u_admin, "setup_bulk", "staff", follow=True)
        self.assertContains(refused, "Choose a role")
        self.assertFalse(Staff.objects.get(pk=self.tara.pk).user_id)
        done = self.post(self.u_admin, "setup_bulk", "staff", data={"role": self.role("teacher").pk})
        self.assertContains(done, "Tara Nair")

    def test_office_cannot_bulk_create_staff(self):
        self.assertEqual(self.post(self.u_office, "setup_bulk", "staff", data={"role": self.role("teacher").pk}).status_code, 403)
        self.assertFalse(Staff.objects.get(pk=self.tara.pk).user_id)


class ImportScreenTests(SetupScreenCase):
    def test_the_start_page_offers_what_you_may_use(self):
        office = self.get(self.u_office, "setup_import")
        self.assertEqual(office.context["can"], {"students": True, "staff": False})
        self.assertNotContains(office, "file-staff")
        self.assertTrue(self.get(self.u_admin, "setup_import").context["can"]["staff"])

    def test_upload_preview_and_apply(self):
        response = self.upload(self.u_office, students_csv({}, {"admission_no": "N1002", "first_name": "Sib"}))
        batch = ImportBatch.objects.get()
        self.assertRedirects(response, reverse("setup_import_preview", args=[batch.pk]))
        preview = self.get(self.u_office, "setup_import_preview", batch.pk)
        self.assertContains(preview, "Students to create")
        self.assertContains(preview, "Ira Shah")
        self.assertContains(preview, "Raj Shah (9811111111)")
        self.assertEqual(Student.objects.filter(admission_no__startswith="N100").count(), 0)  # nothing yet
        done = self.post(self.u_office, "setup_import_preview", batch.pk, follow=True)
        self.assertContains(done, "Imported 2 students")
        self.assertEqual(Student.objects.filter(admission_no__startswith="N100").count(), 2)
        self.assertContains(done, "Students imported")

    def test_apply_can_create_parent_logins_and_show_the_slips(self):
        self.upload(self.u_office, students_csv({}, {"admission_no": "N1002", "first_name": "Sib"}, {"admission_no": "N1003", "guardian_phone": "9833333333", "guardian_name": "Mina Roy"}))
        batch = ImportBatch.objects.get()
        response = self.post(self.u_office, "setup_import_preview", batch.pk, data={"create_logins": "on"})
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertContains(response, "3 students were imported and 2 parent logins created")  # siblings share one parent
        self.assertEqual(len(SLIP_PASSWORD.findall(response.content.decode())), 2)
        self.assertEqual(User.objects.filter(phone__in=["9811111111", "9833333333"]).count(), 2)

    def test_applying_twice_is_refused_with_a_message(self):
        self.upload(self.u_office, students_csv({}))
        batch = ImportBatch.objects.get()
        self.post(self.u_office, "setup_import_preview", batch.pk)
        again = self.post(self.u_office, "setup_import_preview", batch.pk, follow=True)
        self.assertContains(again, "already been applied")
        self.assertEqual(Student.objects.filter(admission_no="N1001").count(), 1)

    def test_problems_are_shown_with_a_file_to_fix(self):
        self.upload(self.u_office, students_csv({"admission_no": "OK"}, {"admission_no": "BAD", "class": "Class XI"}, {"admission_no": "BAD2", "section": "Q"}))
        batch = ImportBatch.objects.get()
        preview = self.get(self.u_office, "setup_import_preview", batch.pk)
        self.assertContains(preview, "Unknown class")
        self.assertContains(preview, "has no section")
        self.assertContains(preview, "Rows with problems")
        download = self.get(self.u_office, "setup_import_problems", batch.pk)
        self.assertEqual(download["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("rows-to-fix", download["Content-Disposition"])
        self.assertEqual(len(list(csv.reader(io.StringIO(download.content.decode())))), 3)

    def test_a_file_with_nothing_importable(self):
        self.upload(self.u_office, students_csv({"admission_no": "A1"}))  # already exists
        preview = self.get(self.u_office, "setup_import_preview", ImportBatch.objects.get().pk)
        self.assertContains(preview, "Nothing can be imported")
        self.assertContains(preview, "Already in the system")
        self.assertNotContains(preview, "<button class=\"btn block\"")

    def test_bad_uploads_get_friendly_messages(self):
        for content, fragment in ((b"", "empty"), (students_csv({}, header=["admission_no", "first_name"]), "missing the column"), (b"\x81\x8d\x8f", "CSV UTF-8")):
            self.assertContains(self.upload(self.u_office, content, follow=True), fragment)
        self.client.force_login(self.u_office)
        no_file = self.client.post(reverse("setup_import"), {"kind": "students"}, follow=True)
        self.assertContains(no_file, "Choose a CSV file")
        self.assertEqual(ImportBatch.objects.count(), 0)

    def test_unknown_kind_and_missing_rights(self):
        self.client.force_login(self.u_office)
        self.assertEqual(self.client.post(reverse("setup_import"), {"kind": "robots", "file": SimpleUploadedFile("a.csv", b"x")}).status_code, 404)
        self.assertEqual(self.upload(self.u_office, staff_csv({}), kind="staff").status_code, 403)  # staff imports are the principal's
        self.assertEqual(ImportBatch.objects.count(), 0)
        for user in (self.u_t1, self.u_p1, self.u_accountant):
            self.assertEqual(self.upload(user, students_csv({})).status_code, 403, user.username)

    def test_the_principal_imports_staff_and_then_makes_logins(self):
        self.upload(self.u_admin, staff_csv({}, {"employee_code": "T901", "first_name": "Lata"}), kind="staff")
        batch = ImportBatch.objects.get()
        self.assertContains(self.get(self.u_admin, "setup_import_preview", batch.pk), "Staff to create")
        done = self.post(self.u_admin, "setup_import_preview", batch.pk, follow=True)
        self.assertContains(done, "Imported 2 staff")
        self.assertEqual(Staff.objects.filter(employee_code__in=["T900", "T901"]).count(), 2)
        self.assertContains(done, "Create staff logins")

    def test_other_schools_batches_do_not_exist(self):
        batch = importing.stage_students(self.school_b, self.u_admin_b, "b.csv", students_csv({"class": "Class 5", "section": "A"}))
        for name in ("setup_import_preview", "setup_import_problems"):
            self.assertEqual(self.status(self.u_office, name, batch.pk), 404)
        self.assertEqual(self.post(self.u_office, "setup_import_preview", batch.pk).status_code, 404)

    def test_a_batch_needs_the_right_permission_for_its_kind(self):
        batch = importing.stage_staff(self.school_a, self.u_admin, "s.csv", staff_csv({}))
        self.assertEqual(self.status(self.u_office, "setup_import_preview", batch.pk), 403)
        self.assertEqual(self.post(self.u_office, "setup_import_preview", batch.pk).status_code, 403)
        self.assertFalse(Staff.objects.filter(employee_code="T900").exists())

    def test_templates_download(self):
        for word, header in (("students", "admission_no"), ("staff", "employee_code")):
            response = self.get(self.u_admin, "setup_import_template", word)
            self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
            self.assertTrue(response.content.decode().startswith(header))
        self.assertEqual(self.status(self.u_office, "setup_import_template", "staff"), 403)
        self.assertEqual(self.status(self.u_admin, "setup_import_template", "robots"), 404)

    def test_names_in_other_scripts_survive_the_whole_journey(self):
        self.upload(self.u_office, students_csv({"first_name": "आरव", "last_name": "शर्मा", "guardian_name": "राजेश शर्मा"}))
        self.post(self.u_office, "setup_import_preview", ImportBatch.objects.get().pk)
        self.assertEqual(Student.objects.get(admission_no="N1001").first_name, "आरव")
        self.assertEqual(Guardian.objects.get(phone="9811111111").last_name, "शर्मा")


class AuditAndAdminTests(SetupScreenCase):
    def test_actions_appear_on_the_hub_with_who_did_them(self):
        self.post(self.u_office, "setup_create_login", "guardians", self.nila.pk)
        self.post(self.u_office, "setup_account_action", self.u_p2.pk, "deactivate")
        hub = self.get(self.u_admin, "setup_home")
        events = {e.action: e for e in hub.context["events"]}
        self.assertEqual(set(events), {"created", "deactivated"})
        self.assertContains(hub, "Login created")
        self.assertContains(hub, "Nila Rao")

    def test_other_schools_events_stay_private(self):
        AccountEvent.objects.create(school=self.school_b, target_label="Secret Person", action="created")
        self.assertNotContains(self.get(self.u_admin, "setup_home"), "Secret Person")

    def test_the_audit_log_and_imports_are_read_only_in_the_admin(self):
        self.post(self.u_office, "setup_create_login", "guardians", self.nila.pk)
        self.client.force_login(self.u_admin)
        for model in ("accountevent", "importbatch"):
            self.assertEqual(self.client.get(reverse(f"admin:onboarding_{model}_add")).status_code, 403)
        event = AccountEvent.objects.get()
        self.assertEqual(self.client.post(reverse("admin:onboarding_accountevent_delete", args=[event.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:onboarding_accountevent_changelist")).status_code, 200)
        self.assertTrue(AccountEvent.objects.filter(pk=event.pk).exists())

    def test_a_school_never_sees_another_schools_events_in_the_admin(self):
        AccountEvent.objects.create(school=self.school_b, target_label="Secret Person", action="created")
        self.client.force_login(self.u_admin)
        self.assertNotContains(self.client.get(reverse("admin:onboarding_accountevent_changelist")), "Secret Person")
