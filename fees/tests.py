import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from academics.models import AcademicYear, Enrollment
from core.models import School
from core.testing import PASSWORD, WorldTestCase
from people.models import Student
from web.templatetags.money import inr, inr0, inr_words

from . import services
from .models import (
    Concession, FeeError, FeeHead, FeeInvoice, FeeInvoiceLine, FeeStructure, NumberSequence, Payment, PaymentAllocation,
)

D = Decimal
TODAY = date(2026, 11, 4)  # a Wednesday in November: billing month index 7 of the 2026-27 session
OCT, NOV = date(2026, 10, 1), date(2026, 11, 1)


class FeeCase(WorldTestCase):
    """Fixture reminder: school A has Class 5 (Asha, Bimal in 5A, Chitra in 5B) at Rs 1000/month tuition and
    Class 6 (Dev) at Rs 2000/month. School B has Zoya in a class with no fee structure."""

    def setUp(self):
        super().setUp()
        patcher = patch("fees.services.today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def tuition(self):
        return FeeHead.objects.get(school=self.school_a, code="TUI")

    def add_structure(self, grade, code, name, amount, frequency, school=None, year=None):
        school = school or self.school_a
        head, _ = FeeHead.objects.get_or_create(school=school, code=code, defaults={"name": name})
        return FeeStructure.objects.create(
            school=school, academic_year=year or self.year_a, grade=grade, head=head, amount=D(amount), frequency=frequency
        )

    def generate(self, month=NOV, school=None, year=None, **kw):
        return services.generate_invoices(school or self.school_a, year or self.year_a, month, self.u_accountant, **kw)

    def invoice(self, student, month=NOV):
        return FeeInvoice.objects.get(student=student, month=month, status="issued")

    def pay(self, student, amount, *, on=TODAY, method="cash", key=None, reference=""):
        return services.record_payment(
            self.school_a, student, amount, method, on, self.u_accountant, idempotency_key=key, reference=reference
        )[0]

    def balance(self, invoice):
        return services.with_balances(FeeInvoice.objects.filter(pk=invoice.pk)).get().balance

    def concede(self, student, kind, value, head=None, reason="Test", **kw):
        return Concession.objects.create(
            school=self.school_a, student=student, academic_year=kw.pop("year", self.year_a), head=head,
            kind=kind, value=D(value), reason=reason, **kw
        )


# =============================================================================================
# Pure helpers
# =============================================================================================

class AmountTests(FeeCase):
    def test_rounding_is_half_up(self):
        self.assertEqual(services.money(D("2.675")), D("2.68"))
        self.assertEqual(services.money(D("75.075")), D("75.08"))
        self.assertEqual(services.money(D("0.004")), D("0.00"))

    def test_typed_amounts_are_parsed_strictly(self):
        self.assertEqual(services.parse_amount("2500"), D("2500.00"))
        self.assertEqual(services.parse_amount(" 1,250.50 "), D("1250.50"))
        for bad in ("", "abc", "-5", "0", "0.00", "12.345", "NaN", "inf", "1e3x"):
            with self.assertRaises(FeeError, msg=bad):
                services.parse_amount(bad)


class RupeeFormattingTests(FeeCase):
    def test_indian_digit_grouping(self):
        self.assertEqual(inr(D("1234567.5")), "₹12,34,567.50")
        self.assertEqual(inr(1000), "₹1,000.00")
        self.assertEqual(inr(0), "₹0.00")
        self.assertEqual(inr(D("99999.99")), "₹99,999.99")
        self.assertEqual(inr(D("100000")), "₹1,00,000.00")
        self.assertEqual(inr(-1500), "-₹1,500.00")

    def test_headline_format_and_junk(self):
        self.assertEqual(inr0(D("999.5")), "₹1,000")
        self.assertEqual(inr0(662560), "₹6,62,560")
        self.assertEqual(inr(None), "")
        self.assertEqual(inr("oops"), "oops")

    def test_amount_in_words(self):
        self.assertEqual(inr_words(D("2200")), "Rupees Two Thousand Two Hundred Only")
        self.assertEqual(inr_words("1250.50"), "Rupees One Thousand Two Hundred Fifty and Fifty Paise Only")
        self.assertEqual(inr_words(125000), "Rupees One Lakh Twenty Five Thousand Only")
        self.assertEqual(inr_words(10000000), "Rupees One Crore Only")
        self.assertEqual(inr_words(0), "Rupees Zero Only")
        self.assertEqual(inr_words(D("19.01")), "Rupees Nineteen and One Paise Only")


class BillingCalendarTests(FeeCase):
    def test_a_session_has_twelve_billing_months_from_april(self):
        months = services.billing_months(self.year_a)
        self.assertEqual((months[0], months[-1], len(months)), (date(2026, 4, 1), date(2027, 3, 1), 12))

    def test_month_index_and_bounds(self):
        self.assertEqual(services.month_index(self.year_a, date(2026, 4, 17)), 0)
        self.assertEqual(services.month_index(self.year_a, date(2027, 3, 1)), 11)
        for outside in (date(2026, 3, 1), date(2027, 4, 1)):
            with self.assertRaises(FeeError):
                services.month_index(self.year_a, outside)

    def test_due_day_is_a_clamped_school_setting(self):
        self.assertEqual(services.due_day(self.school_a), 10)
        for setting, expected in ((5, 5), (31, 28), (0, 1), ("x", 10)):
            school = School(settings={"fee_due_day": setting})
            self.assertEqual(services.due_day(school), expected, setting)


# =============================================================================================
# Generating invoices
# =============================================================================================

class InvoiceGenerationTests(FeeCase):
    def test_bills_every_enrolled_student_from_the_fee_structure(self):
        result = self.generate()
        self.assertEqual((result["created"], result["skipped_existing"], result["skipped_no_fee"]), (4, 0, 0))
        self.assertEqual(result["total"], D("5000.00"))  # 3 x 1000 + 2000
        self.assertEqual(self.invoice(self.asha).total, D("1000.00"))
        self.assertEqual(self.invoice(self.dev).total, D("2000.00"))
        invoice = self.invoice(self.asha)
        self.assertEqual(invoice.due_date, date(2026, 11, 10))
        self.assertEqual([(l.description, l.amount) for l in invoice.lines.all()], [("Tuition Fee", D("1000.00"))])

    def test_invoice_numbers_are_sequential_and_readable(self):
        self.generate()
        numbers = sorted(FeeInvoice.objects.filter(school=self.school_a).values_list("invoice_no", flat=True))
        self.assertEqual(numbers, [f"INV/2026-27/{n:06d}" for n in range(1, 5)])

    def test_running_it_twice_never_double_bills(self):
        self.generate()
        again = self.generate()
        self.assertEqual((again["created"], again["skipped_existing"]), (0, 4))
        self.assertEqual(FeeInvoice.objects.filter(school=self.school_a).count(), 4)
        self.assertEqual(NumberSequence.objects.get(school=self.school_a, key="invoice:2026-27").last_value, 4)

    def test_preview_writes_nothing(self):
        result = self.generate(preview=True)
        self.assertEqual((result["would_create"], result["created"]), (4, 0))
        self.assertEqual(FeeInvoice.objects.count(), 0)
        self.assertEqual(NumberSequence.objects.count(), 0)

    def test_each_month_is_its_own_invoice(self):
        self.generate(OCT)
        self.generate(NOV)
        self.assertEqual(FeeInvoice.objects.filter(student=self.asha).count(), 2)

    def test_students_who_have_left_or_are_not_enrolled_are_not_billed(self):
        Student.objects.filter(pk=self.bimal.pk).update(status="transferred")
        Enrollment.objects.filter(student=self.chitra).delete()
        self.generate()
        self.assertEqual(set(FeeInvoice.objects.values_list("student__first_name", flat=True)), {"Asha", "Dev"})

    def test_another_schools_students_are_never_touched(self):
        self.generate()
        self.assertFalse(FeeInvoice.objects.filter(student=self.zoya).exists())
        result = self.generate(school=self.school_b, year=self.year_b)  # school B has no fee structure at all
        self.assertEqual((result["created"], result["skipped_no_fee"]), (0, 1))

    def test_a_class_with_no_fee_structure_is_skipped_not_billed_zero(self):
        FeeStructure.objects.filter(grade=self.grade6).delete()
        result = self.generate()
        self.assertEqual((result["created"], result["skipped_no_fee"]), (3, 1))

    def test_months_outside_the_session_are_refused(self):
        with self.assertRaises(FeeError):
            self.generate(date(2027, 5, 1))

    def test_any_day_in_the_month_selects_that_month(self):
        self.generate(date(2026, 11, 23))
        self.assertEqual(self.invoice(self.asha).month, NOV)

    def test_cancelled_invoices_can_be_regenerated(self):
        self.generate()
        services.cancel_invoice(self.invoice(self.asha), self.u_accountant, "Wrong class")
        result = self.generate()
        self.assertEqual((result["created"], result["skipped_existing"]), (1, 3))
        self.assertEqual(FeeInvoice.objects.filter(student=self.asha).count(), 2)  # one cancelled, one live
        self.assertEqual(FeeInvoice.objects.filter(student=self.asha, status="issued").count(), 1)


class BillingScheduleTests(FeeCase):
    """Which fee heads appear in which month."""

    def setUp(self):
        super().setUp()
        self.add_structure(self.grade5, "ANN", "Annual Charges", "5000", "annual")
        self.add_structure(self.grade5, "LIB", "Library", "600", "quarterly")
        self.add_structure(self.grade5, "EXM", "Exam Fee", "300", "half_yearly")

    def total(self, month):
        self.generate(month)
        return self.invoice(self.asha, month).total

    def test_april_carries_everything(self):
        self.assertEqual(self.total(date(2026, 4, 1)), D("6900.00"))  # 1000 + 5000 + 600 + 300

    def test_ordinary_months_carry_only_monthly_fees(self):
        self.assertEqual(self.total(date(2026, 5, 1)), D("1000.00"))
        self.assertEqual(self.total(date(2026, 8, 1)), D("1000.00"))

    def test_quarterly_months(self):
        self.assertEqual(self.total(date(2026, 7, 1)), D("1600.00"))   # 1000 + 600
        self.assertEqual(self.total(date(2027, 1, 1)), D("1600.00"))   # 9 months in: quarterly, not half-yearly

    def test_half_yearly_repeats_in_october(self):
        self.assertEqual(self.total(date(2026, 10, 1)), D("1900.00"))  # 1000 + 600 + 300


class OneTimeFeeTests(FeeCase):
    def setUp(self):
        super().setUp()
        self.add_structure(self.grade5, "ADM", "Admission Fee", "10000", "one_time")

    def admission_lines(self, student):
        return list(FeeInvoiceLine.objects.filter(invoice__student=student, head__code="ADM").values_list("invoice__month", "amount"))

    def test_new_students_pay_it_once_on_the_first_invoice_from_their_admission_month(self):
        Student.objects.filter(pk=self.asha.pk).update(admission_date=date(2026, 6, 15))
        self.generate(date(2026, 5, 1))
        self.assertEqual(self.admission_lines(self.asha), [])  # before they joined
        self.generate(date(2026, 6, 1))
        self.generate(date(2026, 7, 1))
        self.assertEqual(self.admission_lines(self.asha), [(date(2026, 6, 1), D("10000.00"))])

    def test_students_admitted_in_earlier_years_never_pay_it(self):
        Student.objects.filter(pk=self.bimal.pk).update(admission_date=date(2024, 4, 1))
        self.generate(date(2026, 4, 1))
        self.assertEqual(self.admission_lines(self.bimal), [])
        self.assertEqual(self.admission_lines(self.chitra), [])  # no admission date recorded

    def test_catch_up_runs_in_any_order_never_charge_it_twice(self):
        Student.objects.filter(pk=self.asha.pk).update(admission_date=date(2026, 6, 15))
        self.generate(date(2026, 7, 1))  # July first
        self.generate(date(2026, 6, 1))  # then June
        self.assertEqual(self.admission_lines(self.asha), [(date(2026, 7, 1), D("10000.00"))])

    def test_a_cancelled_invoice_releases_the_one_time_fee(self):
        Student.objects.filter(pk=self.asha.pk).update(admission_date=date(2026, 6, 15))
        self.generate(date(2026, 6, 1))
        services.cancel_invoice(self.invoice(self.asha, date(2026, 6, 1)), self.u_accountant, "redo")
        self.generate(date(2026, 6, 1))
        live = FeeInvoiceLine.objects.filter(invoice__student=self.asha, invoice__status="issued", head__code="ADM")
        self.assertEqual(live.count(), 1)


class ConcessionTests(FeeCase):
    def lines(self, month=NOV):
        self.generate(month)
        return {(l.description, l.amount) for l in self.invoice(self.asha, month).lines.all()}

    def test_percentage_concession(self):
        self.concede(self.asha, "percent", "10", head=self.tuition, reason="Sibling")
        self.assertEqual(self.lines(), {("Tuition Fee", D("1000.00")), ("Concession: Sibling", D("-100.00"))})
        self.assertEqual(self.invoice(self.asha).total, D("900.00"))
        self.assertEqual(self.invoice(self.bimal).total, D("1000.00"))  # other students unaffected

    def test_fixed_concession_comes_off_every_invoice(self):
        self.concede(self.asha, "fixed", "250")
        self.generate(OCT)
        self.generate(NOV)
        self.assertEqual(self.invoice(self.asha, OCT).total, D("750.00"))
        self.assertEqual(self.invoice(self.asha, NOV).total, D("750.00"))

    def test_rounding_is_half_up_to_the_paisa(self):
        head = self.tuition
        structure = FeeStructure(head=head, amount=D("1001"), frequency="monthly", head_id=head.id)
        c = Concession(kind="percent", value=D("7.5"), head_id=None, reason="x")
        (charge, discount) = services.build_lines([structure], 0, set(), [c])
        self.assertEqual(discount["amount"], D("-75.08"))  # 75.075 rounds up

    def test_a_concession_can_never_make_an_invoice_negative(self):
        self.concede(self.asha, "percent", "60", reason="A")
        self.concede(self.asha, "percent", "60", reason="B")
        self.generate()
        # Both together would be 120%: the second is capped, the invoice reaches exactly zero and is not issued.
        self.assertFalse(FeeInvoice.objects.filter(student=self.asha).exists())
        self.assertEqual(FeeInvoice.objects.filter(school=self.school_a).count(), 3)

    def test_two_concessions_combine(self):
        self.concede(self.asha, "percent", "10", reason="A")
        self.concede(self.asha, "fixed", "50", reason="B")
        self.generate()
        self.assertEqual(self.invoice(self.asha).total, D("850.00"))

    def test_concession_on_a_head_that_is_not_billed_this_month_does_nothing(self):
        annual = self.add_structure(self.grade5, "ANN", "Annual Charges", "5000", "annual")
        self.concede(self.asha, "percent", "50", head=annual.head, reason="Annual only")
        self.assertEqual(self.lines(NOV), {("Tuition Fee", D("1000.00"))})  # annual fee isn't in November

    def test_head_specific_concession_only_touches_its_head(self):
        annual = self.add_structure(self.grade5, "ANN", "Annual Charges", "5000", "annual")
        self.concede(self.asha, "percent", "10", head=annual.head, reason="Annual")
        self.generate(date(2026, 4, 1))
        self.assertEqual(self.invoice(self.asha, date(2026, 4, 1)).total, D("5500.00"))  # 6000 - 500

    def test_inactive_and_other_year_concessions_are_ignored(self):
        self.concede(self.asha, "percent", "50", is_active=False)
        other_year = AcademicYear.objects.create(
            school=self.school_a, name="2025-26", start_date=date(2025, 4, 1), end_date=date(2026, 3, 31)
        )
        self.concede(self.asha, "percent", "50", year=other_year)
        self.generate()
        self.assertEqual(self.invoice(self.asha).total, D("1000.00"))

    def test_concession_validation(self):
        from django.core.exceptions import ValidationError

        for kind, value in (("percent", "0"), ("percent", "101"), ("fixed", "-5")):
            with self.assertRaises(ValidationError, msg=(kind, value)):
                Concession(school=self.school_a, student=self.asha, academic_year=self.year_a, kind=kind, value=D(value), reason="x").clean()

    def test_concession_cannot_mix_schools(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            Concession(school=self.school_a, student=self.zoya, academic_year=self.year_a, value=D("5"), reason="x").save()


# =============================================================================================
# Payments
# =============================================================================================

class PaymentTests(FeeCase):
    def setUp(self):
        super().setUp()
        self.generate(OCT)  # Asha owes 1000 due 10 Oct (overdue as of 4 Nov) ...
        self.generate(NOV)  # ... and 1000 due 10 Nov
        self.oct, self.nov = self.invoice(self.asha, OCT), self.invoice(self.asha, NOV)

    def test_payments_go_to_the_oldest_invoice_first(self):
        payment = self.pay(self.asha, "1500")
        self.assertEqual(self.balance(self.oct), D("0.00"))
        self.assertEqual(self.balance(self.nov), D("500.00"))
        self.assertEqual([(a.invoice.month, a.amount) for a in payment.allocations.all()], [(OCT, D("1000.00")), (NOV, D("500.00"))])
        self.assertEqual(payment.status, "completed")

    def test_oldest_means_oldest_due_date_not_oldest_created(self):
        self.generate(date(2026, 9, 1))  # created last, but due first (10 Sep)
        payment = self.pay(self.asha, "500")
        self.assertEqual([a.invoice.month for a in payment.allocations.all()], [date(2026, 9, 1)])

    def test_part_payments_accumulate(self):
        self.pay(self.asha, "400")
        self.pay(self.asha, "700")
        self.assertEqual(self.balance(self.oct), D("0.00"))
        self.assertEqual(self.balance(self.nov), D("900.00"))

    def test_paying_exactly_everything_clears_the_account(self):
        self.pay(self.asha, "2000")
        account = services.student_account(self.asha)
        self.assertEqual((account["outstanding"], account["overdue"], account["paid"]), (D("0.00"), D("0.00"), D("2000.00")))

    def test_paying_more_than_is_owed_is_refused(self):
        with self.assertRaisesMessage(FeeError, "more than the outstanding dues"):
            self.pay(self.asha, "2000.01")
        self.assertEqual(Payment.objects.count(), 0)

    def test_bad_amounts_dates_and_methods_are_refused(self):
        for amount in ("0", "-100", "abc", "10.555", ""):
            with self.assertRaises(FeeError, msg=amount):
                self.pay(self.asha, amount)
        with self.assertRaisesMessage(FeeError, "future"):
            self.pay(self.asha, "100", on=TODAY + timedelta(days=1))
        with self.assertRaises(FeeError):
            self.pay(self.asha, "100", method="bitcoin")
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_student_with_no_dues_cannot_pay(self):
        newcomer = Student.objects.create(school=self.school_a, admission_no="NEW1", first_name="Newcomer")  # never invoiced
        with self.assertRaisesMessage(FeeError, "no dues"):
            self.pay(newcomer, "100")

    def test_another_schools_student_is_refused(self):
        with self.assertRaises(FeeError):
            services.record_payment(self.school_a, self.zoya, "100", "cash", TODAY, self.u_accountant)

    def test_receipt_numbers_are_sequential_and_per_school(self):
        first, second = self.pay(self.asha, "100"), self.pay(self.asha, "100")
        self.assertEqual((first.receipt_no, second.receipt_no), ("RCPT/2026-27/000001", "RCPT/2026-27/000002"))
        # School B counts its own receipts from 1, independent of school A.
        self.add_structure(self.grade5_b, "TUI", "Tuition Fee", "500", "monthly", school=self.school_b, year=self.year_b)
        self.generate(NOV, school=self.school_b, year=self.year_b)
        payment_b, _ = services.record_payment(self.school_b, self.zoya, "500", "cash", TODAY, self.u_admin_b)
        self.assertEqual(payment_b.receipt_no, "RCPT/2026-27/000001")

    def test_the_same_form_submitted_twice_records_one_payment(self):
        first, created = services.record_payment(self.school_a, self.asha, "300", "upi", TODAY, self.u_accountant, idempotency_key="tok-1")
        again, created_again = services.record_payment(self.school_a, self.asha, "300", "upi", TODAY, self.u_accountant, idempotency_key="tok-1")
        self.assertEqual((created, created_again, again.pk), (True, False, first.pk))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(PaymentAllocation.objects.count(), 1)

    def test_a_reused_key_with_a_different_amount_is_refused(self):
        services.record_payment(self.school_a, self.asha, "300", "upi", TODAY, self.u_accountant, idempotency_key="tok-1")
        with self.assertRaisesMessage(FeeError, "already used"):
            services.record_payment(self.school_a, self.asha, "301", "upi", TODAY, self.u_accountant, idempotency_key="tok-1")

    def test_payment_amount_and_details_cannot_be_edited(self):
        payment = self.pay(self.asha, "500")
        payment.amount = D("5.00")
        with self.assertRaisesMessage(FeeError, "cannot be changed"):
            payment.save()
        payment.refresh_from_db()
        payment.receipt_no = "RCPT/HACKED"
        with self.assertRaises(FeeError):
            payment.save()

    def test_allocations_cannot_be_edited(self):
        allocation = self.pay(self.asha, "500").allocations.get()
        allocation.amount = D("1.00")
        with self.assertRaises(FeeError):
            allocation.save()

    def test_database_refuses_a_zero_payment(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(school=self.school_a, student=self.asha, receipt_no="X", amount=D("0"), method="cash", received_on=TODAY)


class ReversalTests(FeeCase):
    def setUp(self):
        super().setUp()
        self.generate(OCT)
        self.generate(NOV)
        self.payment = self.pay(self.asha, "1500")

    def test_reversing_reopens_the_dues_and_keeps_the_record(self):
        services.reverse_payment(self.payment, self.u_accountant, "Cheque bounced")
        self.payment.refresh_from_db()
        self.assertEqual((self.payment.status, self.payment.reversal_reason, self.payment.reversed_by), ("reversed", "Cheque bounced", self.u_accountant))
        self.assertIsNotNone(self.payment.reversed_at)
        self.assertEqual(services.student_account(self.asha)["outstanding"], D("2000.00"))
        self.assertEqual(Payment.objects.count(), 1)  # nothing deleted
        self.assertEqual(PaymentAllocation.objects.count(), 2)

    def test_a_reason_is_required_and_a_payment_can_only_be_reversed_once(self):
        for blank in ("", "   ", None):
            with self.assertRaises(FeeError):
                services.reverse_payment(self.payment, self.u_accountant, blank)
        services.reverse_payment(self.payment, self.u_accountant, "Wrong student")
        with self.assertRaisesMessage(FeeError, "already been reversed"):
            services.reverse_payment(self.payment, self.u_accountant, "again")

    def test_money_can_be_taken_again_after_a_reversal(self):
        services.reverse_payment(self.payment, self.u_accountant, "Bounced")
        again = self.pay(self.asha, "2000")
        self.assertEqual(again.receipt_no, "RCPT/2026-27/000002")  # numbers are never reused
        self.assertEqual(services.student_account(self.asha)["outstanding"], D("0.00"))

    def test_reversed_money_is_not_counted_anywhere(self):
        before = services.collection_summary(self.school_a, TODAY)
        self.assertEqual(before["collected_this_month"], D("1500.00"))
        services.reverse_payment(self.payment, self.u_accountant, "Bounced")
        after = services.collection_summary(self.school_a, TODAY)
        self.assertEqual(after["collected_this_month"], D("0.00"))
        self.assertEqual(after["outstanding"], before["outstanding"] + D("1500.00"))


class InvoiceCancellationTests(FeeCase):
    def setUp(self):
        super().setUp()
        self.generate()
        self.inv = self.invoice(self.asha)

    def test_an_unpaid_invoice_can_be_cancelled_with_a_reason(self):
        services.cancel_invoice(self.inv, self.u_accountant, "Wrong class billed")
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.status, self.inv.cancel_reason, self.inv.cancelled_by), ("cancelled", "Wrong class billed", self.u_accountant))
        self.assertEqual(services.student_account(self.asha)["outstanding"], D("0.00"))
        self.assertNotIn(self.asha.pk, [r["student"].pk for r in services.dues_report(self.school_a)])

    def test_a_cancelled_invoice_cannot_be_paid(self):
        services.cancel_invoice(self.inv, self.u_accountant, "x")
        with self.assertRaisesMessage(FeeError, "no dues"):
            self.pay(self.asha, "100")

    def test_an_invoice_with_money_against_it_cannot_be_cancelled(self):
        payment = self.pay(self.asha, "100")
        with self.assertRaisesMessage(FeeError, "already been paid"):
            services.cancel_invoice(self.inv, self.u_accountant, "x")
        services.reverse_payment(payment, self.u_accountant, "undo")
        services.cancel_invoice(self.inv, self.u_accountant, "now fine")

    def test_reason_required_and_only_once(self):
        with self.assertRaises(FeeError):
            services.cancel_invoice(self.inv, self.u_accountant, " ")
        services.cancel_invoice(self.inv, self.u_accountant, "x")
        with self.assertRaisesMessage(FeeError, "already cancelled"):
            services.cancel_invoice(self.inv, self.u_accountant, "y")


class LedgerImmutabilityTests(FeeCase):
    def setUp(self):
        super().setUp()
        self.generate()
        self.inv = self.invoice(self.asha)

    def test_issued_invoice_amounts_are_frozen(self):
        for field, value in (("total", D("1.00")), ("due_date", date(2030, 1, 1)), ("invoice_no", "INV/FAKE"), ("month", OCT)):
            invoice = FeeInvoice.objects.get(pk=self.inv.pk)
            setattr(invoice, field, value)
            with self.assertRaisesMessage(FeeError, "cannot be changed"):
                invoice.save()

    def test_invoice_lines_cannot_be_edited(self):
        line = self.inv.lines.get()
        line.amount = D("1.00")
        with self.assertRaisesMessage(FeeError, "cannot be edited"):
            line.save()

    def test_unchanged_saves_still_work(self):
        invoice = FeeInvoice.objects.get(pk=self.inv.pk)
        invoice.save()  # no change to frozen fields: allowed (this is how cancellation updates the status)

    def test_one_live_invoice_per_student_per_month_is_enforced_by_the_database(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            FeeInvoice.objects.create(
                school=self.school_a, student=self.asha, academic_year=self.year_a, month=NOV, invoice_no="INV/DUP",
                due_date=date(2026, 11, 10), total=D("1.00"),
            )


class StatusAndReportTests(FeeCase):
    def test_invoice_status(self):
        self.generate(OCT)
        inv = self.invoice(self.asha, OCT)  # due 10 Oct
        past, before = date(2026, 11, 4), date(2026, 10, 1)
        self.assertEqual(services.invoice_status(inv, D("0"), past), ("overdue", "Overdue"))
        self.assertEqual(services.invoice_status(inv, D("400"), past)[0], "overdue")  # part-paid but late
        self.assertEqual(services.invoice_status(inv, D("400"), before), ("partial", "Part paid"))
        self.assertEqual(services.invoice_status(inv, D("0"), before), ("due", "Due"))
        self.assertEqual(services.invoice_status(inv, D("1000"), past), ("paid", "Paid"))
        inv.status = "cancelled"
        self.assertEqual(services.invoice_status(inv, D("0"), past), ("cancelled", "Cancelled"))

    def test_student_account_totals(self):
        self.generate(OCT)
        self.generate(NOV)
        self.pay(self.asha, "1200")
        account = services.student_account(self.asha)
        self.assertEqual((account["billed"], account["paid"], account["outstanding"], account["overdue"]),
                         (D("2000.00"), D("1200.00"), D("800.00"), D("0.00")))  # October is cleared; November not yet due
        self.assertEqual(account["next_due"], date(2026, 11, 10))
        self.assertEqual([r["status"] for r in account["rows"]], ["partial", "paid"])  # newest first

    def test_balances_for_many_students_in_one_go(self):
        self.generate(OCT)
        self.pay(self.asha, "250")
        result = services.balances_for([self.asha.pk, self.bimal.pk, self.zoya.pk])
        self.assertEqual(result[self.asha.pk]["outstanding"], D("750.00"))
        self.assertEqual(result[self.asha.pk]["overdue"], D("750.00"))
        self.assertEqual(result[self.bimal.pk]["outstanding"], D("1000.00"))
        self.assertEqual(result[self.zoya.pk]["outstanding"], D("0.00"))
        self.assertIsNone(result[self.zoya.pk]["next_due"])

    def test_dues_report_orders_filters_and_stays_in_school(self):
        self.generate(OCT)
        self.generate(NOV)
        self.pay(self.asha, "1000")
        rows = services.dues_report(self.school_a)
        self.assertEqual([r["student"].first_name for r in rows], ["Dev", "Bimal", "Chitra", "Asha"])  # biggest dues first
        self.assertEqual(rows[0]["outstanding"], D("4000.00"))
        self.assertEqual(rows[0]["class_label"], "Class 6-A")
        self.assertEqual([r["student"].first_name for r in services.dues_report(self.school_a, grade_id=self.grade6.pk)], ["Dev"])
        overdue = services.dues_report(self.school_a, overdue_only=True)
        self.assertEqual({r["student"].first_name: r["outstanding"] for r in overdue}, {"Dev": D("2000.00"), "Bimal": D("1000.00"), "Chitra": D("1000.00")})
        self.assertEqual(services.dues_report(self.school_b), [])

    def test_collection_summary(self):
        self.generate(OCT)
        self.generate(NOV)  # billed in November: 5000
        self.pay(self.asha, "1500", on=date(2026, 11, 3))
        self.pay(self.bimal, "400", on=date(2026, 10, 20))  # collected in October: not this month
        summary = services.collection_summary(self.school_a, TODAY)
        self.assertEqual(summary["billed_this_month"], D("5000.00"))
        self.assertEqual(summary["collected_this_month"], D("1500.00"))
        self.assertEqual(summary["outstanding"], D("10000.00") - D("1900.00"))  # 8,100
        self.assertEqual(summary["overdue"], D("5000.00") - D("1400.00"))  # October invoices less what went to them
        self.assertEqual(summary["students_with_dues"], 4)
        self.assertEqual(services.collection_summary(self.school_b, TODAY)["outstanding"], D("0.00"))


# =============================================================================================
# Screens and access
# =============================================================================================

class FeeScreenCase(FeeCase):
    def setUp(self):
        super().setUp()
        self.generate(OCT)
        self.generate(NOV)

    def get(self, user, name, *args, **params):
        self.client.force_login(user)
        return self.client.get(reverse(name, args=args), params)

    def post(self, user, name, *args, data=None, **kw):
        self.client.force_login(user)
        return self.client.post(reverse(name, args=args), data or {}, **kw)

    def status(self, user, name, *args):
        return self.get(user, name, *args).status_code


class AccessMatrixTests(FeeScreenCase):
    def setUp(self):
        super().setUp()
        self.payment = self.pay(self.asha, "500")
        self.asha_invoice = self.invoice(self.asha, OCT)
        self.bimal_invoice = self.invoice(self.bimal, OCT)

    def test_login_is_required_everywhere(self):
        pages = [("fee_home",), ("fee_structure",), ("fee_dues",), ("fee_collect",), ("fee_generate",),
                 ("fee_student", self.asha.pk), ("fee_invoice", self.asha_invoice.pk), ("fee_receipt", self.payment.pk)]
        for name, *args in pages:
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse("login"), response["Location"])

    def test_parents_see_only_their_own_childrens_money(self):
        p1 = self.u_p1  # parent of Asha and Chitra
        self.assertEqual(self.status(p1, "fee_home"), 200)
        self.assertEqual(self.status(p1, "fee_structure"), 200)
        self.assertEqual(self.status(p1, "fee_student", self.asha.pk), 200)
        self.assertEqual(self.status(p1, "fee_student", self.chitra.pk), 200)  # sibling
        self.assertEqual(self.status(p1, "fee_student", self.bimal.pk), 404)
        self.assertEqual(self.status(p1, "fee_invoice", self.asha_invoice.pk), 200)
        self.assertEqual(self.status(p1, "fee_invoice", self.bimal_invoice.pk), 404)
        self.assertEqual(self.status(p1, "fee_receipt", self.payment.pk), 200)
        self.assertEqual(self.status(self.u_p2, "fee_receipt", self.payment.pk), 404)

    def test_parents_cannot_use_staff_money_actions(self):
        for name in ("fee_collect", "fee_generate", "fee_dues"):
            self.assertEqual(self.status(self.u_p1, name), 403, name)
        self.assertEqual(self.status(self.u_p1, "fee_collect_student", self.asha.pk), 403)
        self.assertEqual(self.post(self.u_p1, "fee_generate").status_code, 403)
        self.assertEqual(self.post(self.u_p1, "fee_receipt_reverse", self.payment.pk, data={"reason": "x"}).status_code, 403)
        self.assertEqual(self.post(self.u_p1, "fee_invoice_cancel", self.bimal_invoice.pk, data={"reason": "x"}).status_code, 403)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "completed")

    def test_students_and_teachers_have_no_fee_access(self):
        for user in (self.u_student, self.u_t1, self.u_t2):
            for name in ("fee_home", "fee_structure", "fee_dues", "fee_collect"):
                self.assertEqual(self.status(user, name), 403, (user.username, name))

    def test_accountant_can_do_everything(self):
        for name in ("fee_home", "fee_structure", "fee_dues", "fee_collect", "fee_generate"):
            self.assertEqual(self.status(self.u_accountant, name), 200, name)
        self.assertEqual(self.status(self.u_accountant, "fee_student", self.dev.pk), 200)
        self.assertEqual(self.status(self.u_accountant, "fee_collect_student", self.asha.pk), 200)

    def test_office_can_collect_and_look_but_not_generate_or_undo(self):
        for name in ("fee_home", "fee_dues", "fee_collect"):
            self.assertEqual(self.status(self.u_office, name), 200, name)
        self.assertEqual(self.status(self.u_office, "fee_generate"), 403)
        self.assertEqual(self.post(self.u_office, "fee_receipt_reverse", self.payment.pk, data={"reason": "x"}).status_code, 403)
        self.assertEqual(self.post(self.u_office, "fee_invoice_cancel", self.bimal_invoice.pk, data={"reason": "x"}).status_code, 403)

    def test_principal_can_do_everything(self):
        for name in ("fee_home", "fee_dues", "fee_collect", "fee_generate"):
            self.assertEqual(self.status(self.u_admin, name), 200, name)

    def test_nothing_crosses_schools(self):
        self.assertEqual(self.status(self.u_admin_b, "fee_student", self.asha.pk), 404)
        self.assertEqual(self.status(self.u_admin_b, "fee_invoice", self.asha_invoice.pk), 404)
        self.assertEqual(self.status(self.u_admin_b, "fee_receipt", self.payment.pk), 404)
        self.assertEqual(self.status(self.u_accountant, "fee_student", self.zoya.pk), 404)
        self.assertEqual(self.status(self.u_accountant, "fee_collect_student", self.zoya.pk), 404)
        self.assertEqual(self.post(self.u_admin_b, "fee_receipt_reverse", self.payment.pk, data={"reason": "x"}).status_code, 404)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "completed")

    def test_state_changing_urls_refuse_get(self):
        self.assertEqual(self.status(self.u_accountant, "fee_receipt_reverse", self.payment.pk), 405)
        self.assertEqual(self.status(self.u_accountant, "fee_invoice_cancel", self.bimal_invoice.pk), 405)

    def test_the_structure_page_moved_to_its_own_address(self):
        self.assertEqual(reverse("fee_structure"), "/fees/structure/")
        self.assertEqual(reverse("fee_home"), "/fees/")


class HomeScreenTests(FeeScreenCase):
    def test_staff_overview(self):
        self.pay(self.asha, "500")
        response = self.get(self.u_accountant, "fee_home")
        summary = response.context["summary"]
        self.assertEqual(summary["billed_this_month"], D("5000.00"))
        self.assertEqual(summary["collected_this_month"], D("500.00"))
        self.assertTrue(response.context["can_collect"] and response.context["can_manage"])
        self.assertEqual(len(response.context["recent_payments"]), 1)
        self.assertContains(response, "Collect payment")

    def test_office_overview_has_no_generate_button(self):
        response = self.get(self.u_office, "fee_home")
        self.assertTrue(response.context["can_collect"])
        self.assertFalse(response.context["can_manage"])
        self.assertNotContains(response, "Generate invoices")

    def test_parent_sees_what_each_child_owes(self):
        response = self.get(self.u_p1, "fee_home")
        kids = {k.first_name: k.fee for k in response.context["kids"]}
        self.assertEqual(set(kids), {"Asha", "Chitra"})
        self.assertEqual(kids["Asha"]["outstanding"], D("2000.00"))
        self.assertEqual(kids["Asha"]["overdue"], D("1000.00"))  # October
        self.assertNotIn("summary", response.context)
        self.assertContains(response, "₹2,000")
        self.assertNotContains(response, "Bimal")

    def test_dashboards_show_money_to_those_who_may_see_it(self):
        self.client.force_login(self.u_admin)
        admin_dash = self.client.get(reverse("dashboard"))
        self.assertEqual(admin_dash.context["fees_summary"]["billed_this_month"], D("5000.00"))
        self.client.force_login(self.u_p1)
        parent_dash = self.client.get(reverse("dashboard"))
        self.assertContains(parent_dash, "Fees due")
        self.client.force_login(self.u_t1)
        self.assertNotIn("fees_summary", self.client.get(reverse("dashboard")).context)

    def test_nav_has_a_fees_tab_only_for_roles_with_fee_access(self):
        for user, expected in ((self.u_p1, True), (self.u_accountant, True), (self.u_t1, False), (self.u_student, False)):
            self.client.force_login(user)
            html = self.client.get(reverse("dashboard")).content.decode()
            self.assertEqual('href="/fees/"' in html, expected, user.username)


class StudentAccountScreenTests(FeeScreenCase):
    def test_account_lists_invoices_and_payments(self):
        self.pay(self.asha, "1500", reference="UPI123")
        response = self.get(self.u_p1, "fee_student", self.asha.pk)
        account = response.context["account"]
        self.assertEqual((account["outstanding"], account["paid"]), (D("500.00"), D("1500.00")))
        self.assertContains(response, "INV/2026-27/000001")
        self.assertContains(response, "RCPT/2026-27/000001")
        self.assertContains(response, "₹1,500.00")
        self.assertNotContains(response, "Collect payment")  # parents can't collect

    def test_staff_get_a_collect_button_only_while_something_is_owed(self):
        self.assertContains(self.get(self.u_accountant, "fee_student", self.asha.pk), "Collect payment")
        self.pay(self.asha, "2000")
        self.assertNotContains(self.get(self.u_accountant, "fee_student", self.asha.pk), "Collect payment")


class InvoiceScreenTests(FeeScreenCase):
    def test_invoice_document(self):
        inv = self.invoice(self.asha, OCT)
        response = self.get(self.u_p1, "fee_invoice", inv.pk)
        self.assertContains(response, "FEE INVOICE")
        self.assertContains(response, "INV/2026-27/000001")
        self.assertContains(response, "Tuition Fee")
        self.assertContains(response, "₹1,000.00")
        self.assertContains(response, "Overdue")
        self.assertEqual(response.context["balance"], D("1000.00"))

    def test_concession_shows_as_a_credit_line(self):
        self.concede(self.asha, "percent", "10", reason="Sibling")
        self.generate(date(2026, 9, 1))
        response = self.get(self.u_p1, "fee_invoice", self.invoice(self.asha, date(2026, 9, 1)).pk)
        self.assertContains(response, "Concession: Sibling")
        self.assertContains(response, "-₹100.00")

    def test_cancel_button_only_for_managers_and_only_while_unpaid(self):
        inv = self.invoice(self.asha, OCT)
        self.assertContains(self.get(self.u_accountant, "fee_invoice", inv.pk), "Cancel this invoice")
        self.assertNotContains(self.get(self.u_office, "fee_invoice", inv.pk), "Cancel this invoice")
        self.assertNotContains(self.get(self.u_p1, "fee_invoice", inv.pk), "Cancel this invoice")
        self.pay(self.asha, "100")
        self.assertNotContains(self.get(self.u_accountant, "fee_invoice", inv.pk), "Cancel this invoice")

    def test_cancelling(self):
        inv = self.invoice(self.bimal, OCT)
        response = self.post(self.u_accountant, "fee_invoice_cancel", inv.pk, data={"reason": "Wrong class"}, follow=True)
        self.assertContains(response, "was cancelled")
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.cancel_reason), ("cancelled", "Wrong class"))
        self.assertContains(response, "Wrong class")

    def test_cancelling_needs_a_reason_and_an_unpaid_invoice(self):
        inv = self.invoice(self.asha, OCT)
        blank = self.post(self.u_accountant, "fee_invoice_cancel", inv.pk, data={"reason": ""}, follow=True)
        self.assertContains(blank, "give a reason")
        self.pay(self.asha, "100")
        paid = self.post(self.u_accountant, "fee_invoice_cancel", inv.pk, data={"reason": "x"}, follow=True)
        self.assertContains(paid, "already been paid")
        inv.refresh_from_db()
        self.assertEqual(inv.status, "issued")


class CollectScreenTests(FeeScreenCase):
    def form(self, **overrides):
        data = {"amount": "1000", "method": "upi", "reference": "UPI9", "received_on": TODAY.isoformat(), "notes": "", "token": "t-1"}
        data.update(overrides)
        return data

    def collect(self, data, user=None, student=None):
        return self.post(user or self.u_accountant, "fee_collect_student", (student or self.asha).pk, data=data, follow=True)

    def test_search_finds_students_with_their_balance(self):
        response = self.get(self.u_accountant, "fee_collect", q="asha")
        self.assertEqual([s.first_name for s in response.context["results"]], ["Asha"])
        self.assertEqual(response.context["results"][0].fee["outstanding"], D("2000.00"))
        self.assertEqual(self.get(self.u_accountant, "fee_collect", q="A4").context["results"][0].first_name, "Dev")
        self.assertEqual(self.get(self.u_accountant, "fee_collect", q="zoya").context["results"], [])  # other school
        self.assertEqual(self.get(self.u_accountant, "fee_collect").context["results"], [])

    def test_the_form_is_prefilled_with_the_full_amount_and_a_one_time_token(self):
        response = self.get(self.u_accountant, "fee_collect_student", self.asha.pk)
        self.assertContains(response, 'value="2000.00"')
        token = response.context["form"]["token"]
        self.assertEqual(len(token), 32)
        self.assertNotEqual(token, self.get(self.u_accountant, "fee_collect_student", self.asha.pk).context["form"]["token"])

    def test_recording_a_payment_ends_at_the_receipt(self):
        response = self.collect(self.form())
        self.assertContains(response, "Payment recorded")
        self.assertContains(response, "FEE RECEIPT")
        self.assertContains(response, "RCPT/2026-27/000001")
        self.assertContains(response, "Rupees One Thousand Only")
        payment = Payment.objects.get()
        self.assertEqual((payment.amount, payment.method, payment.reference, payment.received_by), (D("1000.00"), "upi", "UPI9", self.u_accountant))
        self.assertEqual(self.balance(self.invoice(self.asha, OCT)), D("0.00"))

    def test_double_submitting_the_form_does_not_double_count(self):
        self.collect(self.form())
        again = self.collect(self.form())  # same token: a double-click or a browser re-send
        self.assertContains(again, "already recorded")
        self.assertEqual(Payment.objects.count(), 1)

    def test_a_too_large_amount_shows_an_error_and_keeps_what_was_typed(self):
        response = self.collect(self.form(amount="2500", reference="KEEP-ME"))
        self.assertContains(response, "more than the outstanding")
        self.assertContains(response, "KEEP-ME")
        self.assertEqual(Payment.objects.count(), 0)

    def test_bad_input_is_explained(self):
        self.assertContains(self.collect(self.form(amount="abc")), "Enter the amount as a number")
        self.assertContains(self.collect(self.form(amount="0")), "more than zero")
        self.assertContains(self.collect(self.form(received_on="garbage")), "valid payment date")
        self.assertContains(self.collect(self.form(received_on=(TODAY + timedelta(days=2)).isoformat())), "future")
        self.assertContains(self.collect(self.form(method="barter")), "how the payment was made")
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_student_with_nothing_due_gets_a_friendly_page(self):
        FeeInvoice.objects.filter(student=self.bimal).update(status="cancelled")
        self.assertContains(self.get(self.u_accountant, "fee_collect_student", self.bimal.pk), "no dues right now")

    def test_office_staff_may_collect(self):
        self.assertContains(self.collect(self.form(), user=self.u_office), "Payment recorded")

    def test_online_is_not_a_hand_entered_method(self):
        page = self.get(self.u_accountant, "fee_collect_student", self.asha.pk)
        offered = [code for code, _label in page.context["methods"]]
        self.assertEqual(set(offered), {"cash", "upi", "cheque", "bank_transfer", "card", "other"})
        self.assertContains(self.collect(self.form(method="online")), "how the payment was made")  # forged form value
        self.assertEqual(Payment.objects.count(), 0)


class ReceiptScreenTests(FeeScreenCase):
    def setUp(self):
        super().setUp()
        self.payment = self.pay(self.asha, "1250.50", method="cheque", reference="CHQ777")

    def test_receipt_document(self):
        response = self.get(self.u_p1, "fee_receipt", self.payment.pk)
        for text in ("FEE RECEIPT", "RCPT/2026-27/000001", "₹1,250.50", "Cheque", "CHQ777",
                     "Rupees One Thousand Two Hundred Fifty and Fifty Paise Only", "INV/2026-27/000001"):
            self.assertContains(response, text)
        self.assertNotContains(response, "Reverse this payment")  # parents can't reverse

    def test_reversing(self):
        response = self.post(self.u_accountant, "fee_receipt_reverse", self.payment.pk, data={"reason": "Cheque bounced"}, follow=True)
        self.assertContains(response, "was reversed")
        self.assertContains(response, "REVERSED")
        self.assertContains(response, "Cheque bounced")
        self.assertContains(response, "no longer valid")
        self.assertNotContains(response, "Reverse this payment")  # only once
        self.assertEqual(services.student_account(self.asha)["outstanding"], D("2000.00"))
        # parents see the reversal too
        self.assertContains(self.get(self.u_p1, "fee_receipt", self.payment.pk), "no longer valid")

    def test_reversal_needs_a_reason(self):
        response = self.post(self.u_accountant, "fee_receipt_reverse", self.payment.pk, data={"reason": " "}, follow=True)
        self.assertContains(response, "give a reason")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "completed")

    def test_only_managers_see_the_reverse_form(self):
        self.assertContains(self.get(self.u_accountant, "fee_receipt", self.payment.pk), "Reverse this payment")
        self.assertNotContains(self.get(self.u_office, "fee_receipt", self.payment.pk), "Reverse this payment")


class GenerateScreenTests(FeeCase):
    def get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse("fee_generate"), params)

    def test_preview_then_create(self):
        response = self.get(self.u_accountant)
        self.assertEqual(response.context["chosen"], NOV)  # defaults to the current month
        self.assertEqual(response.context["preview"]["would_create"], 4)
        self.assertEqual(FeeInvoice.objects.count(), 0)  # looking changes nothing
        self.client.force_login(self.u_accountant)
        done = self.client.post(reverse("fee_generate"), {"month": "2026-11"}, follow=True)
        self.assertContains(done, "Created 4 invoices for November 2026")
        self.assertEqual(FeeInvoice.objects.filter(status="issued").count(), 4)
        again = self.client.post(reverse("fee_generate"), {"month": "2026-11"}, follow=True)
        self.assertContains(again, "Created 0 invoices")
        self.assertEqual(FeeInvoice.objects.count(), 4)

    def test_only_months_up_to_now_can_be_billed(self):
        months = self.get(self.u_accountant).context["months"]
        self.assertEqual((months[0], months[-1], len(months)), (date(2026, 4, 1), NOV, 8))
        future = self.get(self.u_accountant, month="2027-01")
        self.assertEqual(future.context["chosen"], NOV)  # falls back instead of billing the future

    def test_garbage_month_is_harmless(self):
        for junk in ("junk", "2026-99", "", "2026"):
            self.assertEqual(self.get(self.u_accountant, month=junk).status_code, 200, junk)

    def test_a_bad_month_is_refused_not_silently_replaced(self):
        self.client.force_login(self.u_accountant)
        for bad in ("2027-02", "junk", "", "2026-99"):
            response = self.client.post(reverse("fee_generate"), {"month": bad}, follow=True)
            self.assertContains(response, "choose one of the listed months", msg_prefix=bad)
        self.assertEqual(FeeInvoice.objects.count(), 0)  # in particular, nothing was billed for the "default" month


class DuesScreenTests(FeeScreenCase):
    def setUp(self):
        super().setUp()
        self.pay(self.asha, "1000")

    def test_listing_and_filters(self):
        response = self.get(self.u_accountant, "fee_dues")
        self.assertEqual([r["student"].first_name for r in response.context["rows"]], ["Dev", "Bimal", "Chitra", "Asha"])
        self.assertEqual(response.context["total_outstanding"], D("10000.00") - D("1000.00"))
        by_class = self.get(self.u_accountant, "fee_dues", grade=self.grade6.pk)
        self.assertEqual([r["student"].first_name for r in by_class.context["rows"]], ["Dev"])
        overdue = self.get(self.u_accountant, "fee_dues", overdue="1")
        self.assertEqual(overdue.context["total_overdue"], D("5000.00") - D("1000.00"))
        self.assertEqual(self.get(self.u_accountant, "fee_dues", grade="abc").status_code, 200)

    def test_csv_export(self):
        response = self.get(self.u_accountant, "fee_dues", format="csv")
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0][:4], ["Admission no", "Student", "Class", "Outstanding (INR)"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[1][:5], ["A4", "Dev Test", "Class 6-A", "4000.00", "2000.00"])

    def test_csv_cannot_smuggle_spreadsheet_formulas(self):
        Student.objects.filter(pk=self.asha.pk).update(first_name='=HYPERLINK("http://evil.example","x")')
        response = self.get(self.u_accountant, "fee_dues", format="csv")
        names = [row[1] for row in csv.reader(io.StringIO(response.content.decode()))][1:]
        self.assertTrue(any(name.startswith("'=HYPERLINK") for name in names))
        self.assertFalse(any(name.startswith("=") for name in names))

    def test_empty_state(self):
        FeeInvoice.objects.update(status="cancelled")
        self.assertContains(self.get(self.u_accountant, "fee_dues"), "Everything has been paid")


class PerformanceTests(FeeScreenCase):
    def count_queries(self, user, name, *args):
        self.client.force_login(user)
        self.client.get(reverse(name, args=args))  # warm-up (the first request also saves the session)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        return len(ctx)

    def test_dues_list_query_count_does_not_grow_with_the_number_of_students(self):
        before = self.count_queries(self.u_accountant, "fee_dues")
        for i in range(15):
            s = Student.objects.create(school=self.school_a, admission_no=f"P{i}", first_name=f"Extra{i}")
            Enrollment.objects.create(school=self.school_a, student=s, section=self.sec5a, roll_no=50 + i)
        self.generate(OCT)
        self.assertEqual(self.count_queries(self.u_accountant, "fee_dues"), before)

    def test_parent_home_query_count_is_flat(self):
        before = self.count_queries(self.u_p1, "fee_home")
        self.pay(self.asha, "300")
        self.pay(self.asha, "300")
        self.assertEqual(self.count_queries(self.u_p1, "fee_home"), before)


class AdminSiteTests(FeeScreenCase):
    def test_ledger_is_read_only_even_for_the_platform_owner(self):
        self.pay(self.asha, "500")
        owner = get_user_model().objects.create_superuser("owner", password=PASSWORD)
        for user in (self.u_admin, owner):
            self.client.force_login(user)
            for model in ("feeinvoice", "payment"):
                self.assertEqual(self.client.get(reverse(f"admin:fees_{model}_add")).status_code, 403, (user.username, model))
        invoice = self.invoice(self.asha, OCT)
        self.client.force_login(self.u_admin)
        self.assertEqual(self.client.post(reverse("admin:fees_feeinvoice_delete", args=[invoice.pk]), {"post": "yes"}).status_code, 403)
        self.assertTrue(FeeInvoice.objects.filter(pk=invoice.pk).exists())

    def test_office_staff_can_view_the_ledger_for_their_school_only(self):
        self.client.force_login(self.u_office)
        response = self.client.get(reverse("admin:fees_feeinvoice_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INV/2026-27/000001")

    def test_accountant_manages_concessions_and_the_school_cannot_be_forged(self):
        self.client.force_login(self.u_accountant)
        response = self.client.post(
            reverse("admin:fees_concession_add"),
            {"student": self.asha.pk, "academic_year": self.year_a.pk, "kind": "percent", "value": "15", "reason": "Scholarship",
             "is_active": "on", "school": self.school_b.pk},
        )
        self.assertEqual(response.status_code, 302)
        concession = Concession.objects.get(reason="Scholarship")
        self.assertEqual(concession.school, self.school_a)

    def test_office_cannot_edit_concessions(self):
        self.client.force_login(self.u_office)
        self.assertEqual(self.client.get(reverse("admin:fees_concession_add")).status_code, 403)

    def test_a_concession_added_later_only_affects_future_invoices(self):
        before = self.invoice(self.asha, NOV).total
        self.concede(self.asha, "percent", "50")
        self.assertEqual(self.invoice(self.asha, NOV).total, before)  # issued invoices are frozen
        self.generate(date(2026, 9, 1))
        self.assertEqual(self.invoice(self.asha, date(2026, 9, 1)).total, D("500.00"))
