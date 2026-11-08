from decimal import Decimal

from django.test import SimpleTestCase

from core.money import format_inr


class FormatInrTests(SimpleTestCase):
    def test_indian_digit_grouping(self):
        self.assertEqual(format_inr("1234567.5"), "₹12,34,567.50")
        self.assertEqual(format_inr(Decimal("125000")), "₹1,25,000.00")
        self.assertEqual(format_inr(999), "₹999.00")
        self.assertEqual(format_inr(1000, 0), "₹1,000")

    def test_rounds_half_up_to_the_places_asked(self):
        self.assertEqual(format_inr("2.005"), "₹2.01")
        self.assertEqual(format_inr("125000.5", 0), "₹1,25,001")

    def test_negative_blank_and_junk(self):
        self.assertEqual(format_inr("-1500"), "-₹1,500.00")
        self.assertEqual(format_inr(None), "")
        self.assertEqual(format_inr(""), "")
        self.assertEqual(format_inr("abc"), "abc")
