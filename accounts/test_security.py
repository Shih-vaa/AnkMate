from unittest.mock import patch
import secrets
import time

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from accounts import throttle
from core.testing import PASSWORD, WorldTestCase

NEW_PASSWORD = secrets.token_urlsafe(12)  # generated per run: no password is written into the source


class FirstSignInTests(WorldTestCase):
    """Anyone still on a temporary password must choose their own before they can use anything else."""

    def setUp(self):
        super().setUp()
        self.u_p1.must_change_password = True
        self.u_p1.save()

    def login(self, user=None, password=PASSWORD):
        self.client.logout()
        return self.client.post(reverse("login"), {"username": (user or self.u_p1).username, "password": password}, follow=True)

    def test_signing_in_leads_straight_to_the_change_password_page(self):
        response = self.login()
        self.assertEqual(response.redirect_chain[-1][0], reverse("password_change"))
        self.assertContains(response, "Choose your own password")
        self.assertContains(response, "temporary password")

    def test_every_other_page_redirects_there(self):
        self.client.force_login(self.u_p1)
        for name in ("dashboard", "student_list", "attendance_home", "fee_home", "marks_home", "fee_structure"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertEqual(response["Location"], reverse("password_change"), name)
        post = self.client.post(reverse("fee_collect"), {})
        self.assertEqual(post.status_code, 302)

    def test_the_few_pages_it_needs_stay_reachable(self):
        self.client.force_login(self.u_p1)
        self.assertEqual(self.client.get(reverse("password_change")).status_code, 200)
        for name in ("manifest", "service_worker", "offline"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertNotEqual(self.client.get("/static/web/app.css").status_code, 302)  # static files are not redirected
        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)  # logged out: to the login page
        self.assertIn(reverse("login"), self.client.get(reverse("dashboard"))["Location"])

    def test_choosing_a_new_password_clears_the_flag_and_opens_the_app(self):
        self.client.force_login(self.u_p1)
        response = self.client.post(
            reverse("password_change"),
            {"old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}, follow=True,
        )
        self.assertEqual(response.redirect_chain[-1][0], reverse("dashboard"))
        self.assertContains(response, "Your password has been changed")
        self.u_p1.refresh_from_db()
        self.assertFalse(self.u_p1.must_change_password)
        self.assertTrue(self.u_p1.check_password(NEW_PASSWORD))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)  # still signed in, and free to roam

    def test_the_new_password_must_be_different_and_acceptable(self):
        self.client.force_login(self.u_p1)
        url = reverse("password_change")
        same = self.client.post(url, {"old_password": PASSWORD, "new_password1": PASSWORD, "new_password2": PASSWORD})
        self.assertContains(same, "different from the current one")
        for bad, fragment in (("12345678", "entirely numeric"), ("short", "too short"), ("password", "too common")):
            response = self.client.post(url, {"old_password": PASSWORD, "new_password1": bad, "new_password2": bad})
            self.assertContains(response, fragment, msg_prefix=bad)
        mismatch = self.client.post(url, {"old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD + "x"})
        self.assertContains(mismatch, "two password fields")
        wrong_old = self.client.post(url, {"old_password": "wrong", "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        self.assertContains(wrong_old, "incorrect")
        self.u_p1.refresh_from_db()
        self.assertTrue(self.u_p1.must_change_password)  # nothing changed

    def test_people_with_their_own_password_are_unaffected(self):
        self.client.force_login(self.u_p2)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        page = self.client.get(reverse("password_change"))
        self.assertContains(page, "Change password")  # a voluntary change
        self.assertNotContains(page, "temporary password")

    def test_the_change_page_needs_a_login(self):
        self.client.logout()
        response = self.client.get(reverse("password_change"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_the_voluntary_page_is_linked_from_the_navigation(self):
        self.client.force_login(self.u_p2)
        self.assertContains(self.client.get(reverse("dashboard")), reverse("password_change"))


class LockoutTests(WorldTestCase):
    def attempt(self, identifier="t1", password="wrong", address="10.0.0.1"):
        return self.client.post(reverse("login"), {"username": identifier, "password": password}, REMOTE_ADDR=address)

    def test_five_wrong_passwords_lock_the_account_even_for_the_right_one(self):
        for _ in range(5):
            self.assertContains(self.attempt(), "Please enter a correct")
        locked = self.attempt(password=PASSWORD)  # the right password is refused while locked
        self.assertContains(locked, "Too many sign-in attempts")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_four_wrong_attempts_are_not_enough(self):
        for _ in range(4):
            self.attempt()
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 302)  # signed in

    def test_a_successful_sign_in_resets_the_count(self):
        for _ in range(4):
            self.attempt()
        self.attempt(password=PASSWORD)
        self.client.logout()
        for _ in range(4):
            self.attempt()
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 302)  # still never reached five in a row

    def test_other_accounts_are_not_affected(self):
        for _ in range(6):
            self.attempt("t1")
        self.assertEqual(self.attempt("t2", PASSWORD).status_code, 302)

    def test_another_address_is_not_locked_by_one_bad_neighbour(self):
        for _ in range(6):
            self.attempt("t1", address="10.0.0.1")
        self.assertEqual(self.attempt("t1", PASSWORD, address="10.0.0.2").status_code, 302)

    def test_rotating_addresses_does_not_help_a_guesser(self):
        for n in range(15):  # three times the per-address limit, each from a different address
            self.attempt("t1", address=f"10.0.1.{n}")
        self.assertContains(self.attempt("t1", PASSWORD, address="10.0.9.9"), "Too many sign-in attempts")

    def test_a_phone_number_counts_the_same_however_it_is_typed(self):
        for form in ("9876543210", "+91 98765 43210", "98765-43210", "+919876543210", "9876543210"):
            self.attempt(form)
        self.assertContains(self.attempt("9876543210", PASSWORD), "Too many sign-in attempts")

    def test_unknown_accounts_lock_too_so_nothing_is_revealed(self):
        for _ in range(5):
            self.attempt("nobody-here")
        self.assertContains(self.attempt("nobody-here"), "Too many sign-in attempts")

    def test_the_lock_expires(self):
        for _ in range(5):
            self.attempt()
        self.assertContains(self.attempt(password=PASSWORD), "Too many sign-in attempts")
        with patch("time.time", return_value=time.time() + 16 * 60):
            self.assertEqual(self.attempt(password=PASSWORD).status_code, 302)

    @override_settings(LOGIN_MAX_ATTEMPTS=2)
    def test_the_limit_is_a_setting(self):
        self.attempt()
        self.attempt()
        self.assertContains(self.attempt(password=PASSWORD), "Too many sign-in attempts")

    def test_canonical_identifiers(self):
        self.assertEqual(throttle.canonical(" T1 "), "t1")
        self.assertEqual(throttle.canonical("+91 98765-43210"), "9876543210")
        self.assertEqual(throttle.canonical("Paul@Example.com"), "paul@example.com")
        self.assertEqual(throttle.canonical(None), "")

    def test_a_locked_attempt_is_not_counted_again_as_a_new_failure_window(self):
        for _ in range(5):
            self.attempt()
        before = cache.get("login-fail:t1:10.0.0.1")
        self.attempt()  # refused before the password is even checked
        self.assertEqual(cache.get("login-fail:t1:10.0.0.1"), before)
