from datetime import date
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from academics.models import Enrollment, GradeSubject, TeachingAssignment
from core.models import School
from core.presets.icse import PRESET
from core.testing import WorldTestCase
from people.models import Student

from . import services
from .models import Exam, ExamPaper, GradeBand, Mark, ReportRemark
from .presets import apply_assessment_preset

D = Decimal


class MarksCase(WorldTestCase):
    """Fixture reminder: Class 5 has Mathematics and Science, Class 6 only Science. 5A is taught Maths by t1 (also its class
    teacher), 5B is taught Maths by t2, 6A Science by 'dual' (who is also a parent of Bimal).
    Exams: 'Unit Test' (20% of the result, papers out of 25) and 'Final' (80%, out of 100)."""

    def setUp(self):
        super().setUp()
        for grade, subject in [(self.grade5, self.math), (self.grade5, self.science), (self.grade6, self.science)]:
            GradeSubject.objects.create(school=self.school_a, grade=grade, subject=subject)
        TeachingAssignment.objects.create(school=self.school_a, section=self.sec5a, subject=self.math, teacher=self.t1)
        self.ut = Exam.objects.create(school=self.school_a, academic_year=self.year_a, name="Unit Test", order=1, weightage=D("20"))
        self.final = Exam.objects.create(school=self.school_a, academic_year=self.year_a, name="Final", order=2, weightage=D("80"))
        services.create_papers(self.ut, D("25"))
        services.create_papers(self.final, D("100"))
        for label, minimum in (("A", 80), ("B", 60), ("C", 40), ("D", 33), ("E", 0)):
            GradeBand.objects.create(school=self.school_a, label=label, min_percent=D(minimum))

    # -- helpers -------------------------------------------------------------------------------
    def section_of(self, student):
        return Enrollment.objects.get(student=student, academic_year=self.year_a).section

    def paper(self, exam, student, subject):
        return ExamPaper.objects.get(exam=exam, grade=self.section_of(student).grade, subject=subject)

    def put(self, exam, subject, student, value, user=None):
        """Store a mark directly: value is a Decimal, 'AB' or None."""
        services.save_marks(self.paper(exam, student, subject), self.section_of(student), {student.pk: value}, user or self.u_admin)

    def fill_asha(self):
        """Asha: Maths 20/25 then 60/100 (64.0%); Science 10/25 then 50/100 (48.0%). Overall 56.0%, grade C."""
        self.put(self.ut, self.math, self.asha, D("20"))
        self.put(self.final, self.math, self.asha, D("60"))
        self.put(self.ut, self.science, self.asha, D("10"))
        self.put(self.final, self.science, self.asha, D("50"))

    def publish(self, *exams):
        for exam in exams:
            services.set_published(Exam.objects.get(pk=exam.pk), True)

    def report(self, student=None, *, drafts=False):
        return services.build_report(student or self.asha, self.year_a, self.school_a, include_unpublished=drafts)

    def subject(self, report, name):
        return next(s for s in report["subjects"] if s["subject"].name == name)


# =============================================================================================
# Numbers, grades, parsing
# =============================================================================================

class ParsingAndGradesTests(MarksCase):
    def test_typed_marks(self):
        m = D("100")
        self.assertIsNone(services.parse_mark("", m))
        self.assertIsNone(services.parse_mark("   ", m))
        for word in ("AB", "ab", "Absent", "a", " abs "):
            self.assertEqual(services.parse_mark(word, m), "AB", word)
        self.assertEqual(services.parse_mark("17.5", m), D("17.50"))
        self.assertEqual(services.parse_mark("0", m), D("0.00"))
        self.assertEqual(services.parse_mark("100", m), D("100.00"))

    def test_invalid_marks_are_explained(self):
        m = D("100")
        for bad, fragment in (("abc", "number"), ("-1", "negative"), ("100.01", "more than"), ("12.345", "two decimal"), ("NaN", "number"), ("inf", "number")):
            with self.assertRaisesMessage(ValueError, fragment):
                services.parse_mark(bad, m)

    def test_percentages_round_half_up_to_one_decimal(self):
        self.assertEqual(services.pct(D("72.25")), D("72.3"))
        self.assertEqual(services.pct(D("72.249")), D("72.2"))
        self.assertEqual(services.pct(D("0")), D("0.0"))

    def test_grade_bands_include_the_boundary(self):
        bands = services.grade_bands(self.school_a)
        for percent, expected in (("100", "A"), ("80", "A"), ("79.9", "B"), ("60", "B"), ("59.9", "C"), ("40", "C"),
                                  ("39.9", "D"), ("33", "D"), ("32.9", "E"), ("0", "E")):
            self.assertEqual(services.grade_for(bands, D(percent)), expected, percent)
        self.assertEqual(services.grade_for(bands, None), "")
        self.assertEqual(services.grade_for([], D("90")), "-")  # a school with no grade scale yet

    def test_pass_mark_is_a_clamped_school_setting(self):
        self.assertEqual(services.pass_percent(self.school_a), D("33"))
        for setting, expected in ((40, "40"), ("45.5", "45.5"), (150, "100"), (-5, "0"), ("x", "33")):
            self.assertEqual(services.pass_percent(School(settings={"pass_percent": setting})), D(expected), setting)


# =============================================================================================
# Who may enter marks
# =============================================================================================

class EntryPermissionTests(MarksCase):
    def check(self, user, exam, section, subject):
        return services.can_enter(user, self.school_a, exam, section, subject)

    def test_subject_teacher_enters_their_own_subject_and_class(self):
        self.assertEqual(self.check(self.u_t1, self.ut, self.sec5a, self.math), (True, ""))
        self.assertEqual(self.check(self.u_t2, self.ut, self.sec5b, self.math), (True, ""))
        self.assertEqual(self.check(self.u_dual, self.ut, self.sec6a, self.science), (True, ""))

    def test_not_your_subject_or_class(self):
        ok, why = self.check(self.u_t1, self.ut, self.sec5a, self.science)  # class teacher, but doesn't teach Science
        self.assertFalse(ok)
        self.assertIn("aren't assigned", why)
        self.assertFalse(self.check(self.u_t1, self.ut, self.sec5b, self.math)[0])
        self.assertFalse(self.check(self.u_t2, self.ut, self.sec5a, self.math)[0])

    def test_principal_may_enter_anything(self):
        for section, subject in ((self.sec5a, self.math), (self.sec5b, self.science), (self.sec6a, self.science)):
            self.assertTrue(self.check(self.u_admin, self.ut, section, subject)[0])

    def test_parents_students_and_other_schools_may_not(self):
        for user in (self.u_p1, self.u_student, self.u_admin_b, self.u_accountant):
            self.assertFalse(self.check(user, self.ut, self.sec5a, self.math)[0], user.username)

    def test_no_paper_no_entry(self):
        ok, why = self.check(self.u_admin, self.ut, self.sec6a, self.math)  # Class 6 has no Maths paper
        self.assertFalse(ok)
        self.assertIn("no paper", why)

    def test_publishing_closes_entry_for_teachers_but_not_the_principal(self):
        self.publish(self.ut)
        ok, why = self.check(self.u_t1, Exam.objects.get(pk=self.ut.pk), self.sec5a, self.math)
        self.assertFalse(ok)
        self.assertIn("published", why)
        self.assertTrue(self.check(self.u_admin, Exam.objects.get(pk=self.ut.pk), self.sec5a, self.math)[0])

    def test_locking_closes_entry_for_everyone(self):
        self.publish(self.ut)
        services.lock_exam(Exam.objects.get(pk=self.ut.pk))
        locked = Exam.objects.get(pk=self.ut.pk)
        for user in (self.u_t1, self.u_admin):
            ok, why = self.check(user, locked, self.sec5a, self.math)
            self.assertFalse(ok)
            self.assertIn("locked", why)

    def test_a_section_from_another_year_is_refused(self):
        from academics.models import AcademicYear, Section

        other_year = AcademicYear.objects.create(school=self.school_a, name="2025-26", start_date=date(2025, 4, 1), end_date=date(2026, 3, 31))
        old_section = Section.objects.create(school=self.school_a, academic_year=other_year, grade=self.grade5, name="A")
        self.assertFalse(self.check(self.u_admin, self.ut, old_section, self.math)[0])


class EntryTargetsTests(MarksCase):
    def test_teacher_sees_only_their_assignments_with_progress(self):
        self.put(self.ut, self.math, self.asha, D("20"))
        rows = services.entry_targets(self.u_t1, self.school_a, self.ut)
        self.assertEqual([(str(r["section"]), r["subject"].name, r["entered"], r["total"]) for r in rows], [("Class 5-A", "Mathematics", 1, 2)])

    def test_principal_sees_every_class_and_subject_that_has_a_paper(self):
        rows = services.entry_targets(self.u_admin, self.school_a, self.ut)
        pairs = {(str(r["section"]), r["subject"].name) for r in rows}
        self.assertEqual(pairs, {("Class 5-A", "Mathematics"), ("Class 5-A", "Science"), ("Class 5-B", "Mathematics"),
                                 ("Class 5-B", "Science"), ("Class 6-A", "Science")})

    def test_rows_become_read_only_once_published(self):
        self.publish(self.ut)
        ut = Exam.objects.get(pk=self.ut.pk)
        self.assertFalse(services.entry_targets(self.u_t1, self.school_a, ut)[0]["editable"])
        self.assertTrue(services.entry_targets(self.u_admin, self.school_a, ut)[0]["editable"])

    def test_nobody_else_gets_rows(self):
        for user in (self.u_p1, self.u_student, self.u_accountant, self.u_t3):
            self.assertEqual(services.entry_targets(user, self.school_a, self.ut), [], user.username)


# =============================================================================================
# Saving marks
# =============================================================================================

class SaveMarksTests(MarksCase):
    def setUp(self):
        super().setUp()
        self.p_math = self.paper(self.final, self.asha, self.math)

    def save(self, entries, user=None):
        return services.save_marks(self.p_math, self.sec5a, entries, user or self.u_t1)

    def test_creates_updates_absent_and_clears(self):
        self.assertEqual(self.save({self.asha.pk: D("62.5"), self.bimal.pk: "AB"}), {"saved": 2, "cleared": 0})
        asha, bimal = Mark.objects.get(student=self.asha, paper=self.p_math), Mark.objects.get(student=self.bimal, paper=self.p_math)
        self.assertEqual((asha.marks, asha.absent, asha.entered_by), (D("62.50"), False, self.u_t1))
        self.assertEqual((bimal.marks, bimal.absent), (None, True))
        self.assertEqual(self.save({self.asha.pk: D("70"), self.bimal.pk: None}), {"saved": 1, "cleared": 1})
        self.assertEqual(Mark.objects.get(student=self.asha, paper=self.p_math).marks, D("70.00"))
        self.assertFalse(Mark.objects.filter(student=self.bimal, paper=self.p_math).exists())  # blank = not entered
        self.assertEqual(Mark.objects.filter(paper=self.p_math).count(), 1)  # no duplicates

    def test_blank_boxes_that_never_had_a_mark_are_not_reported_as_cleared(self):
        self.assertEqual(self.save({self.asha.pk: D("50"), self.bimal.pk: None}), {"saved": 1, "cleared": 0})
        self.assertEqual(self.save({self.asha.pk: None, self.bimal.pk: None}), {"saved": 0, "cleared": 1})  # only Asha's existed

    def test_absent_is_not_the_same_as_zero(self):
        self.save({self.asha.pk: D("0"), self.bimal.pk: "AB"})
        self.assertEqual(Mark.objects.get(student=self.asha, paper=self.p_math).marks, D("0.00"))
        self.assertIsNone(Mark.objects.get(student=self.bimal, paper=self.p_math).marks)

    def test_out_of_range_marks_save_nothing_and_name_the_student(self):
        with self.assertRaises(services.MarksInputError) as caught:
            self.save({self.asha.pk: D("50"), self.bimal.pk: D("100.5")})
        self.assertEqual(set(caught.exception.errors), {self.bimal.pk})
        self.assertEqual(Mark.objects.count(), 0)  # Asha's valid 50 was not saved either
        with self.assertRaises(services.MarksInputError):
            self.save({self.asha.pk: D("-1")})

    def test_students_outside_the_class_are_refused(self):
        with self.assertRaisesMessage(services.MarksError, "aren't in this class"):
            self.save({self.asha.pk: D("50"), self.chitra.pk: D("50")})
        self.assertEqual(Mark.objects.count(), 0)

    def test_only_active_enrolled_students_count(self):
        Student.objects.filter(pk=self.bimal.pk).update(status="transferred")
        with self.assertRaises(services.MarksError):
            self.save({self.bimal.pk: D("50")})

    def test_database_refuses_impossible_rows(self):
        for kwargs in ({"marks": D("5"), "absent": True}, {"marks": D("-1")}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Mark.objects.create(school=self.school_a, paper=self.p_math, student=self.asha, **kwargs)

    def test_a_paper_must_have_positive_maximum(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExamPaper.objects.create(school=self.school_a, exam=self.final, grade=self.grade6, subject=self.math, max_marks=D("0"))

    def test_marks_cannot_mix_schools(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            Mark(school=self.school_a, paper=self.p_math, student=self.zoya, marks=D("5")).save()


# =============================================================================================
# Results
# =============================================================================================

class ResultTests(MarksCase):
    def test_subject_result_is_the_weighted_percentage(self):
        self.fill_asha()
        self.publish(self.ut, self.final)
        report = self.report()
        maths, science = self.subject(report, "Mathematics"), self.subject(report, "Science")
        self.assertEqual((maths["percent"], maths["grade"], maths["passed"], maths["complete"]), (D("64.0"), "B", True, True))
        self.assertEqual((science["percent"], science["grade"]), (D("48.0"), "C"))
        self.assertEqual([(c["exam"].name, c["marks"], c["max"], c["percent"]) for c in maths["cells"]],
                         [("Unit Test", D("20.00"), D("25.00"), D("80.0")), ("Final", D("60.00"), D("100.00"), D("60.0"))])

    def test_overall_is_the_plain_average_of_subject_percentages(self):
        self.fill_asha()
        self.publish(self.ut, self.final)
        overall = self.report()["overall"]
        self.assertEqual((overall["percent"], overall["grade"], overall["below_pass"], overall["complete"]), (D("56.0"), "C", [], True))

    def test_exams_not_entered_yet_are_left_out_not_counted_as_zero(self):
        self.put(self.ut, self.math, self.asha, D("20"))  # the Final has no marks yet
        self.publish(self.ut, self.final)
        maths = self.subject(self.report(), "Mathematics")
        self.assertEqual(maths["percent"], D("80.0"))  # 20/25 over the one exam held
        self.assertFalse(maths["complete"])
        self.assertEqual(maths["cells"][1]["state"], "pending")

    def test_absent_counts_as_zero_for_that_exam(self):
        self.put(self.ut, self.math, self.asha, D("20"))
        self.put(self.final, self.math, self.asha, "AB")
        self.publish(self.ut, self.final)
        maths = self.subject(self.report(), "Mathematics")
        self.assertEqual(maths["percent"], D("16.0"))  # (0.8 x 20 + 0 x 80) / 100
        self.assertFalse(maths["passed"])
        self.assertEqual(maths["cells"][1]["state"], "absent")

    def test_pass_and_grade_use_the_rounded_percentage(self):
        # 32.96% displays as 33.0%, so it must be treated as a pass: what is printed agrees with the decision.
        self.put(self.final, self.math, self.asha, D("32.96"))
        self.publish(self.final)
        maths = self.subject(self.report(), "Mathematics")
        self.assertEqual(maths["percent"], D("33.0"))
        self.assertTrue(maths["passed"])
        self.assertEqual(maths["grade"], "D")

    def test_below_pass_subjects_are_listed(self):
        self.put(self.final, self.math, self.asha, D("20"))
        self.put(self.final, self.science, self.asha, D("80"))
        self.publish(self.final)
        overall = self.report()["overall"]
        self.assertEqual(overall["below_pass"], ["Mathematics"])
        self.assertEqual(overall["percent"], D("50.0"))

    def test_optional_subjects_are_shown_but_left_out_of_the_overall(self):
        GradeSubject.objects.filter(grade=self.grade5, subject=self.science).update(is_optional=True)
        self.put(self.final, self.math, self.asha, D("70"))
        self.put(self.final, self.science, self.asha, D("20"))
        self.publish(self.final)
        report = self.report()
        self.assertEqual(report["overall"]["percent"], D("70.0"))
        self.assertTrue(self.subject(report, "Science")["optional"])
        self.assertEqual(report["overall"]["below_pass"], [])

    def test_the_pass_mark_is_a_school_setting(self):
        self.put(self.final, self.math, self.asha, D("40"))
        self.publish(self.final)
        School.objects.filter(pk=self.school_a.pk).update(settings={"pass_percent": 45})
        school = School.objects.get(pk=self.school_a.pk)
        report = services.build_report(self.asha, self.year_a, school)
        self.assertFalse(self.subject(report, "Mathematics")["passed"])

    def test_exam_totals_add_up_across_subjects(self):
        self.fill_asha()
        self.publish(self.ut, self.final)
        totals = {t["exam"].name: t for t in self.report()["exam_totals"]}
        self.assertEqual((totals["Unit Test"]["obtained"], totals["Unit Test"]["maximum"], totals["Unit Test"]["percent"]), (D("30.00"), D("50.00"), D("60.0")))
        self.assertEqual(totals["Final"]["percent"], D("55.0"))  # 110 / 200

    def test_a_student_with_no_enrolment_gets_an_empty_report(self):
        newcomer = Student.objects.create(school=self.school_a, admission_no="N1", first_name="Newcomer")
        report = self.report(newcomer)
        self.assertEqual((report["section"], report["subjects"], report["overall"]), (None, [], None))

    def test_unpublished_exams_are_hidden_unless_drafts_are_requested(self):
        self.fill_asha()
        self.publish(self.ut)  # Final stays a draft
        public, staff = self.report(), self.report(drafts=True)
        self.assertEqual([e.name for e in public["exams"]], ["Unit Test"])
        self.assertEqual(self.subject(public, "Mathematics")["percent"], D("80.0"))  # only the published exam counts
        self.assertEqual([e.name for e in staff["exams"]], ["Unit Test", "Final"])
        self.assertEqual(self.subject(staff, "Mathematics")["percent"], D("64.0"))
        self.assertTrue(staff["has_draft"])
        self.assertFalse(public["has_draft"])

    def test_latest_published_result_per_child(self):
        self.fill_asha()
        self.put(self.ut, self.math, self.bimal, "AB")
        self.publish(self.ut)
        latest = services.latest_results_for([self.asha.pk, self.bimal.pk, self.chitra.pk], self.year_a, self.school_a)
        self.assertEqual((latest[self.asha.pk]["exam"], latest[self.asha.pk]["percent"]), ("Unit Test", D("60.0")))  # 30/50, Final still draft
        self.assertEqual(latest[self.bimal.pk]["percent"], D("0.0"))  # absent counts as zero out of the maximum
        self.assertNotIn(self.chitra.pk, latest)
        self.publish(self.final)
        latest = services.latest_results_for([self.asha.pk], self.year_a, self.school_a)
        self.assertEqual((latest[self.asha.pk]["exam"], latest[self.asha.pk]["percent"]), ("Final", D("55.0")))


class MarksheetTests(MarksCase):
    def setUp(self):
        super().setUp()
        self.put(self.final, self.math, self.asha, D("60"))
        self.put(self.final, self.math, self.bimal, D("20"))

    def sheet(self):
        return services.build_marksheet(self.sec5a, self.final, self.school_a)

    def test_rows_totals_and_pending(self):
        rows = {r["student"].first_name: r for r in self.sheet()["rows"]}
        self.assertEqual((rows["Asha"]["obtained"], rows["Asha"]["maximum"], rows["Asha"]["percent"], rows["Asha"]["grade"]),
                         (D("60.00"), D("100.00"), D("60.0"), "B"))
        self.assertEqual([c["state"] for c in rows["Asha"]["cells"]], ["result", "pending"])  # Maths done, Science not entered
        self.assertFalse(rows["Asha"]["complete"])

    def test_subject_analysis(self):
        stats = {s["paper"].subject.name: s for s in self.sheet()["subject_stats"]}
        maths = stats["Mathematics"]
        self.assertEqual((maths["entered"], maths["average"], maths["highest"], maths["lowest"]), (2, D("40.0"), D("60.0"), D("20.0")))
        self.assertEqual(maths["pass_rate"], D("50.0"))  # 60 passes, 20 doesn't
        self.assertEqual(maths["grades"], [("B", 1), ("E", 1)])
        self.assertEqual(stats["Science"]["entered"], 0)
        self.assertIsNone(stats["Science"]["average"])

    def test_students_below_the_pass_mark_are_flagged(self):
        flagged = [r["student"].first_name for r in self.sheet()["needs_attention"]]
        self.assertEqual(flagged, ["Bimal"])

    def test_absent_students_do_not_drag_the_average_down(self):
        self.put(self.final, self.math, self.bimal, "AB")
        maths = next(s for s in self.sheet()["subject_stats"] if s["paper"].subject.name == "Mathematics")
        self.assertEqual((maths["absent"], maths["average"]), (1, D("60.0")))

    def test_inactive_students_are_not_on_the_sheet(self):
        Student.objects.filter(pk=self.bimal.pk).update(status="transferred")
        self.assertEqual([r["student"].first_name for r in self.sheet()["rows"]], ["Asha"])


class ExamManagementTests(MarksCase):
    def test_publishing_and_unpublishing(self):
        exam = services.set_published(Exam.objects.get(pk=self.ut.pk), True)
        self.assertTrue(exam.is_published)
        self.assertIsNotNone(exam.published_at)
        exam = services.set_published(exam, False)
        self.assertEqual((exam.is_published, exam.published_at), (False, None))

    def test_locking_needs_publication_and_freezes_the_exam(self):
        with self.assertRaisesMessage(services.MarksError, "Publish the exam"):
            services.lock_exam(Exam.objects.get(pk=self.ut.pk))
        self.publish(self.ut)
        locked = services.lock_exam(Exam.objects.get(pk=self.ut.pk))
        self.assertTrue(locked.is_locked)
        with self.assertRaisesMessage(services.MarksError, "locked"):
            services.set_published(locked, False)

    def test_create_papers_is_idempotent_and_leaves_existing_alone(self):
        self.assertEqual(ExamPaper.objects.filter(exam=self.ut).count(), 3)
        ExamPaper.objects.filter(exam=self.ut, grade=self.grade6).delete()
        self.assertEqual(services.create_papers(self.ut, D("30")), 1)  # only the missing one
        self.assertEqual(services.create_papers(self.ut, D("30")), 0)
        self.assertEqual(ExamPaper.objects.get(exam=self.ut, grade=self.grade5, subject=self.math).max_marks, D("25.00"))
        self.assertEqual(ExamPaper.objects.get(exam=self.ut, grade=self.grade6).max_marks, D("30.00"))

    def test_progress(self):
        self.assertEqual(services.exam_progress(self.ut), {"papers": 3, "expected": 7, "entered": 0})  # (2+1) students x 2 subjects + Dev x 1
        self.put(self.ut, self.math, self.asha, D("20"))
        self.put(self.ut, self.science, self.dev, "AB")
        self.assertEqual(services.exam_progress(self.ut)["entered"], 2)

    def test_the_preset_creates_the_exam_structure_once(self):
        School.objects.create(name="Gamma", code="gamma")
        school = School.objects.get(code="gamma")
        from academics.models import AcademicYear, Grade, Subject

        year = AcademicYear.objects.create(school=school, name="2026-27", start_date=date(2026, 4, 1), end_date=date(2027, 3, 31), is_current=True)
        grade, subject = Grade.objects.create(school=school, name="Class 1", order=1), Subject.objects.create(school=school, code="E", name="English")
        GradeSubject.objects.create(school=school, grade=grade, subject=subject)
        apply_assessment_preset(school, year, PRESET)
        apply_assessment_preset(school, year, PRESET)  # running it again changes nothing
        self.assertEqual(list(Exam.objects.filter(school=school).values_list("name", flat=True)), ["Unit Test 1", "Half-Yearly", "Unit Test 2", "Annual"])
        self.assertEqual(Exam.objects.get(school=school, name="Annual").starts_on, date(2027, 3, 1))  # March falls in the next calendar year
        self.assertEqual(ExamPaper.objects.filter(school=school).count(), 4)
        self.assertEqual(GradeBand.objects.filter(school=school).count(), len(PRESET["grade_bands"]))
        self.assertEqual(sum(e.weightage for e in Exam.objects.filter(school=school)), D("100"))


class RemarkTests(MarksCase):
    def test_who_may_write_a_remark(self):
        self.assertTrue(services.can_write_remark(self.u_t1, self.school_a, self.asha, self.year_a))  # class teacher of 5A
        self.assertFalse(services.can_write_remark(self.u_t2, self.school_a, self.asha, self.year_a))  # teaches Maths, but not the class teacher
        self.assertTrue(services.can_write_remark(self.u_admin, self.school_a, self.dev, self.year_a))
        for user in (self.u_p1, self.u_student, self.u_accountant):
            self.assertFalse(services.can_write_remark(user, self.school_a, self.asha, self.year_a), user.username)

    def test_saving_replacing_and_clearing(self):
        services.save_remark(self.asha, self.year_a, "  Works hard.  ", self.u_t1)
        remark = ReportRemark.objects.get(student=self.asha)
        self.assertEqual((remark.text, remark.written_by), ("Works hard.", self.u_t1))
        services.save_remark(self.asha, self.year_a, "Much improved.", self.u_t1)
        self.assertEqual(ReportRemark.objects.filter(student=self.asha).count(), 1)
        services.save_remark(self.asha, self.year_a, "   ", self.u_t1)
        self.assertFalse(ReportRemark.objects.filter(student=self.asha).exists())

    def test_overlong_remarks_are_refused(self):
        with self.assertRaisesMessage(services.MarksError, "300"):
            services.save_remark(self.asha, self.year_a, "x" * 301, self.u_t1)


class DraftVisibilityTests(MarksCase):
    def test_who_may_see_unpublished_results(self):
        sees = lambda user, student: services.can_see_drafts(user, self.school_a, student)
        self.assertTrue(sees(self.u_admin, self.asha))
        self.assertTrue(sees(self.u_t1, self.asha))      # their own class
        self.assertFalse(sees(self.u_t1, self.chitra))   # someone else's class
        self.assertFalse(sees(self.u_p1, self.asha))     # a parent, never
        self.assertFalse(sees(self.u_student, self.asha))
        self.assertFalse(sees(self.u_admin_b, self.asha))

    def test_a_teacher_who_is_also_a_parent_gets_no_drafts_for_their_own_child(self):
        # 'dual' teaches Class 6-A and is a guardian of Bimal (Class 5-A): Bimal's drafts are not theirs to see.
        self.assertTrue(services.can_see_drafts(self.u_dual, self.school_a, self.dev))
        self.assertFalse(services.can_see_drafts(self.u_dual, self.school_a, self.bimal))


# =============================================================================================
# Screens and access
# =============================================================================================

class MarksScreenCase(MarksCase):
    def get(self, user, name, *args, **params):
        self.client.force_login(user)
        return self.client.get(reverse(name, args=args), params)

    def post(self, user, name, *args, data=None, **kw):
        self.client.force_login(user)
        return self.client.post(reverse(name, args=args), data or {}, **kw)

    def status(self, user, name, *args):
        return self.get(user, name, *args).status_code

    def post_marks(self, user, exam, section, subject, values=None, absent=(), **kw):
        data = {f"m_{s.pk}": v for s, v in (values or {}).items()}
        data.update({f"ab_{s.pk}": "on" for s in absent})
        return self.post(user, "marks_enter", exam.pk, section.pk, subject.pk, data=data, follow=True, **kw)


class AnonymousTests(MarksScreenCase):
    def test_everything_needs_a_login(self):
        urls = [reverse("marks_home"), reverse("marks_exams"), reverse("marks_entry_index", args=[self.ut.pk]),
                reverse("marks_enter", args=[self.ut.pk, self.sec5a.pk, self.math.pk]), reverse("marks_sheet_index", args=[self.ut.pk]),
                reverse("marks_sheet", args=[self.ut.pk, self.sec5a.pk]), reverse("marks_student", args=[self.asha.pk]),
                reverse("marks_report", args=[self.asha.pk])]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn(reverse("login"), response["Location"])


class HomeScreenTests(MarksScreenCase):
    def test_teacher_sees_exams_and_can_enter(self):
        response = self.get(self.u_t1, "marks_home")
        self.assertEqual([e.name for e in response.context["exams"]], ["Unit Test", "Final"])
        self.assertTrue(response.context["can_enter"])
        self.assertFalse(response.context["is_principal"])
        self.assertContains(response, "Enter marks")

    def test_principal_sees_progress_and_the_manage_link(self):
        self.put(self.ut, self.math, self.asha, D("20"))
        response = self.get(self.u_admin, "marks_home")
        self.assertEqual(response.context["exams"][0].progress["entered"], 1)
        self.assertContains(response, "Manage exams, publish and lock")

    def test_parent_sees_children_and_latest_published_result(self):
        self.fill_asha()
        response = self.get(self.u_p1, "marks_home")
        kids = {k.first_name: k for k in response.context["kids"]}
        self.assertEqual(set(kids), {"Asha", "Chitra"})
        self.assertIsNone(kids["Asha"].latest)  # nothing published yet
        self.assertNotIn("exams", response.context)
        self.publish(self.ut)
        kids = {k.first_name: k for k in self.get(self.u_p1, "marks_home").context["kids"]}
        self.assertEqual(kids["Asha"].latest["percent"], D("60.0"))
        self.assertContains(self.get(self.u_p1, "marks_home"), "60%")

    def test_roles_without_marks_permissions_are_refused(self):
        self.assertEqual(self.status(self.u_accountant, "marks_home"), 403)
        self.assertEqual(self.status(self.u_office, "marks_home"), 403)

    def test_navigation_shows_marks_to_the_right_roles(self):
        for user, expected in ((self.u_p1, True), (self.u_t1, True), (self.u_admin, True), (self.u_student, True), (self.u_accountant, False)):
            self.client.force_login(user)
            html = self.client.get(reverse("dashboard")).content.decode()
            self.assertEqual(">Marks<" in html, expected, user.username)

    def test_parent_dashboard_card_shows_the_latest_result(self):
        self.fill_asha()
        self.publish(self.ut)
        self.client.force_login(self.u_p1)
        self.assertContains(self.client.get(reverse("dashboard")), "Latest result (Unit Test)")


class EntryScreenTests(MarksScreenCase):
    def test_who_can_open_which_entry_grid(self):
        args = lambda section, subject: (self.ut.pk, section.pk, subject.pk)
        self.assertEqual(self.status(self.u_t1, "marks_enter", *args(self.sec5a, self.math)), 200)
        self.assertEqual(self.status(self.u_t1, "marks_enter", *args(self.sec5a, self.science)), 404)  # not their subject
        self.assertEqual(self.status(self.u_t1, "marks_enter", *args(self.sec5b, self.math)), 404)     # not their class
        self.assertEqual(self.status(self.u_t2, "marks_enter", *args(self.sec5b, self.math)), 200)
        self.assertEqual(self.status(self.u_admin, "marks_enter", *args(self.sec5b, self.science)), 200)
        self.assertEqual(self.status(self.u_admin, "marks_enter", *args(self.sec6a, self.math)), 404)  # no such paper
        for user in (self.u_p1, self.u_student, self.u_accountant):
            self.assertEqual(self.status(user, "marks_enter", *args(self.sec5a, self.math)), 403, user.username)
        self.assertEqual(self.status(self.u_admin_b, "marks_enter", *args(self.sec5a, self.math)), 404)

    def test_the_grid_lists_the_roster(self):
        response = self.get(self.u_t1, "marks_enter", self.ut.pk, self.sec5a.pk, self.math.pk)
        self.assertEqual([r["student"].first_name for r in response.context["rows"]], ["Asha", "Bimal"])
        self.assertTrue(response.context["editable"])
        self.assertContains(response, "out of <strong>25</strong>")

    def test_saving_marks_and_seeing_them_again(self):
        response = self.post_marks(self.u_t1, self.ut, self.sec5a, self.math, {self.asha: "20", self.bimal: "17.5"})
        self.assertContains(response, "Saved 2 marks for Mathematics, Class 5-A")
        self.assertEqual(Mark.objects.get(student=self.bimal, paper=self.paper(self.ut, self.bimal, self.math)).marks, D("17.50"))
        rows = {r["student"].first_name: r for r in response.context["rows"]}
        self.assertEqual((rows["Asha"]["value"], rows["Bimal"]["value"]), ("20", "17.5"))

    def test_absent_by_tick_or_typing_and_blank_clears(self):
        self.post_marks(self.u_t1, self.ut, self.sec5a, self.math, {self.asha: "20", self.bimal: "5"})
        response = self.post_marks(self.u_t1, self.ut, self.sec5a, self.math, {self.asha: "", self.bimal: "ab"})
        self.assertContains(response, "1 cleared")
        paper = self.paper(self.ut, self.asha, self.math)
        self.assertFalse(Mark.objects.filter(student=self.asha, paper=paper).exists())
        self.assertTrue(Mark.objects.get(student=self.bimal, paper=paper).absent)
        self.post_marks(self.u_t1, self.ut, self.sec5a, self.math, {self.asha: "7", self.bimal: "9"}, absent=[self.asha])
        self.assertTrue(Mark.objects.get(student=self.asha, paper=paper).absent)  # the AB tick wins over a typed number

    def test_one_bad_entry_saves_nothing_and_is_pointed_out(self):
        response = self.post_marks(self.u_t1, self.ut, self.sec5a, self.math, {self.asha: "20", self.bimal: "30"})
        self.assertContains(response, "more than the maximum of 25")
        self.assertContains(response, "Nothing was saved yet")
        self.assertEqual(Mark.objects.count(), 0)
        self.assertEqual({r["student"].first_name: r["value"] for r in response.context["rows"]}, {"Asha": "20", "Bimal": "30"})  # typing is kept

    def test_junk_is_explained(self):
        for text, fragment in (("abc", "Enter a number"), ("-3", "negative"), ("1.234", "two decimal places")):
            response = self.post_marks(self.u_t1, self.ut, self.sec5a, self.math, {self.asha: text, self.bimal: "5"})
            self.assertContains(response, fragment, msg_prefix=text)
        self.assertEqual(Mark.objects.count(), 0)

    def test_forged_students_are_ignored(self):
        response = self.post_marks(self.u_t1, self.ut, self.sec5a, self.math, {self.asha: "20", self.bimal: "10", self.chitra: "25", self.zoya: "25"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Mark.objects.count(), 2)
        self.assertFalse(Mark.objects.filter(student__in=[self.chitra, self.zoya]).exists())

    def test_teachers_cannot_post_for_other_classes_or_subjects(self):
        for section, subject in ((self.sec5b, self.math), (self.sec5a, self.science)):
            response = self.post(self.u_t1, "marks_enter", self.ut.pk, section.pk, subject.pk, data={f"m_{self.chitra.pk}": "20"})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(Mark.objects.count(), 0)

    def test_after_publication_teachers_are_read_only_but_the_principal_can_correct(self):
        self.put(self.ut, self.math, self.asha, D("20"))
        self.publish(self.ut)
        page = self.get(self.u_t1, "marks_enter", self.ut.pk, self.sec5a.pk, self.math.pk)
        self.assertFalse(page.context["editable"])
        self.assertContains(page, "already published")
        self.assertNotContains(page, "Save marks")
        blocked = self.post_marks(self.u_t1, self.ut, self.sec5a, self.math, {self.asha: "25", self.bimal: "25"})
        self.assertContains(blocked, "already published")
        self.assertEqual(Mark.objects.get(student=self.asha).marks, D("20.00"))
        self.post_marks(self.u_admin, self.ut, self.sec5a, self.math, {self.asha: "22", self.bimal: ""})
        self.assertEqual(Mark.objects.get(student=self.asha).marks, D("22.00"))
        self.assertEqual(Mark.objects.get(student=self.asha).entered_by, self.u_admin)  # the correction is attributed

    def test_a_locked_exam_cannot_be_changed_by_anyone(self):
        self.put(self.ut, self.math, self.asha, D("20"))
        self.publish(self.ut)
        services.lock_exam(Exam.objects.get(pk=self.ut.pk))
        for user in (self.u_t1, self.u_admin):
            response = self.post_marks(user, self.ut, self.sec5a, self.math, {self.asha: "1", self.bimal: "1"})
            self.assertContains(response, "locked", msg_prefix=user.username)
        self.assertEqual(Mark.objects.get(student=self.asha).marks, D("20.00"))

    def test_entry_index_lists_only_my_classes_and_subjects(self):
        response = self.get(self.u_t1, "marks_entry_index", self.ut.pk)
        self.assertEqual([(str(t["section"]), t["subject"].name) for t in response.context["targets"]], [("Class 5-A", "Mathematics")])
        self.assertEqual(len(self.get(self.u_admin, "marks_entry_index", self.ut.pk).context["targets"]), 5)
        self.assertEqual(self.get(self.u_t3, "marks_entry_index", self.ut.pk).context["targets"], [])  # a teacher with no classes
        self.assertEqual(self.status(self.u_admin_b, "marks_entry_index", self.ut.pk), 404)  # another school's exam


class SheetScreenTests(MarksScreenCase):
    def test_who_can_open_which_class_sheet(self):
        for user, section, expected in ((self.u_t1, self.sec5a, 200), (self.u_t1, self.sec5b, 404), (self.u_t2, self.sec5b, 200),
                                        (self.u_admin, self.sec6a, 200), (self.u_p1, self.sec5a, 404), (self.u_student, self.sec5a, 404),
                                        (self.u_admin_b, self.sec5a, 404)):
            self.assertEqual(self.status(user, "marks_sheet", self.final.pk, section.pk), expected, (user.username, str(section)))
        self.assertEqual(self.status(self.u_p1, "marks_sheet_index", self.final.pk), 404)  # parents get no class lists
        self.assertEqual(self.status(self.u_t1, "marks_sheet_index", self.final.pk), 200)

    def test_sheet_shows_marks_even_for_a_draft_exam_to_staff(self):
        self.put(self.final, self.math, self.asha, D("60"))
        response = self.get(self.u_t1, "marks_sheet", self.final.pk, self.sec5a.pk)
        self.assertContains(response, "Draft: parents can")
        self.assertContains(response, "Asha Test")
        self.assertEqual(response.context["sheet"]["rows"][0]["obtained"], D("60.00"))

    def test_query_count_does_not_grow_with_class_size(self):
        self.client.force_login(self.u_t1)
        url = reverse("marks_sheet", args=[self.final.pk, self.sec5a.pk])
        self.client.get(url)  # warm-up: the first request also saves the session

        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(ctx)

        before = queries()
        for i in range(15):
            s = Student.objects.create(school=self.school_a, admission_no=f"S{i}", first_name=f"Extra{i}")
            Enrollment.objects.create(school=self.school_a, student=s, section=self.sec5a, roll_no=20 + i)
            self.put(self.final, self.math, s, D("50"))
        self.assertEqual(queries(), before)


class StudentScreenTests(MarksScreenCase):
    def setUp(self):
        super().setUp()
        self.fill_asha()
        self.publish(self.ut)  # the Final is still a draft

    def test_parent_sees_only_published_results_of_their_own_children(self):
        response = self.get(self.u_p1, "marks_student", self.asha.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e.name for e in response.context["report"]["exams"]], ["Unit Test"])
        self.assertNotContains(response, "<th class=\"num\">Final")  # the draft exam has no column anywhere on the page
        self.assertNotContains(response, "Includes unpublished")
        self.assertEqual(self.status(self.u_p1, "marks_student", self.chitra.pk), 200)  # sibling
        self.assertEqual(self.status(self.u_p1, "marks_student", self.bimal.pk), 404)
        self.assertEqual(self.status(self.u_p2, "marks_student", self.asha.pk), 404)

    def test_the_class_teacher_sees_drafts_with_a_flag(self):
        response = self.get(self.u_t1, "marks_student", self.asha.pk)
        self.assertEqual([e.name for e in response.context["report"]["exams"]], ["Unit Test", "Final"])
        self.assertContains(response, "Includes unpublished marks")

    def test_teachers_see_only_students_of_their_classes(self):
        self.assertEqual(self.status(self.u_t1, "marks_student", self.asha.pk), 200)
        self.assertEqual(self.status(self.u_t1, "marks_student", self.chitra.pk), 404)
        self.assertEqual(self.status(self.u_admin, "marks_student", self.zoya.pk), 404)  # other school

    def test_a_teacher_who_is_a_parent_sees_published_results_only_for_their_own_child(self):
        response = self.get(self.u_dual, "marks_student", self.bimal.pk)  # Bimal is their child, not their pupil
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["drafts"])

    def test_student_sees_their_own_page_only(self):
        self.assertEqual(self.status(self.u_student, "marks_student", self.asha.pk), 200)
        self.assertEqual(self.status(self.u_student, "marks_student", self.bimal.pk), 404)

    def test_numbers_on_the_page(self):
        response = self.get(self.u_p1, "marks_student", self.asha.pk)
        self.assertContains(response, "Mathematics")
        self.assertContains(response, "<strong>20</strong> / 25")
        self.assertContains(response, "80%")

    def test_nothing_published_says_so(self):
        Exam.objects.update(is_published=False)
        self.assertContains(self.get(self.u_p1, "marks_student", self.asha.pk), "No results have been published yet")


class ReportCardTests(MarksScreenCase):
    def setUp(self):
        super().setUp()
        self.fill_asha()
        self.publish(self.ut, self.final)

    def test_report_card_contents(self):
        services.save_remark(self.asha, self.year_a, "A thoughtful, hard-working student.", self.u_t1)
        response = self.get(self.u_p1, "marks_report", self.asha.pk)
        for text in ("REPORT CARD", "Session 2026-27", "Asha Test", "Mathematics", "Science", "A thoughtful, hard-working student.",
                     "Grade scale", "Unit Test", "Final", "Result:"):
            self.assertContains(response, text)
        self.assertEqual(response.context["report"]["overall"]["percent"], D("56.0"))
        self.assertContains(response, "All subjects at or above the pass mark")

    def test_attendance_appears_on_the_report(self):
        from attendance import services as attendance_services

        attendance_services.save_section_attendance(self.sec5a, date(2026, 11, 3), {self.asha.pk: "P", self.bimal.pk: "A"}, self.u_admin)
        response = self.get(self.u_p1, "marks_report", self.asha.pk)
        self.assertContains(response, "Attendance:")
        self.assertContains(response, "100%")

    def test_parents_never_get_the_unpublished_preview(self):
        Exam.objects.filter(pk=self.final.pk).update(is_published=False)
        response = self.get(self.u_p1, "marks_report", self.asha.pk, preview="1")
        self.assertFalse(response.context["preview"])
        self.assertFalse(response.context["can_preview"])
        self.assertEqual([e.name for e in response.context["report"]["exams"]], ["Unit Test"])  # the unpublished Final is not there
        self.assertNotContains(response, "Preview")

    def test_staff_can_preview_drafts(self):
        Exam.objects.filter(pk=self.final.pk).update(is_published=False)
        plain = self.get(self.u_t1, "marks_report", self.asha.pk)
        self.assertNotContains(plain, "Preview: this includes")
        self.assertContains(plain, "Preview with unpublished marks")
        preview = self.get(self.u_t1, "marks_report", self.asha.pk, preview="1")
        self.assertTrue(preview.context["preview"])
        self.assertContains(preview, "Preview: this includes marks that are not published")
        self.assertContains(preview, "Final")

    def test_who_can_open_a_report_card(self):
        self.assertEqual(self.status(self.u_p1, "marks_report", self.asha.pk), 200)
        self.assertEqual(self.status(self.u_p1, "marks_report", self.bimal.pk), 404)
        self.assertEqual(self.status(self.u_t1, "marks_report", self.chitra.pk), 404)
        self.assertEqual(self.status(self.u_admin_b, "marks_report", self.asha.pk), 404)

    def test_the_class_teacher_writes_the_remark(self):
        response = self.post(self.u_t1, "marks_remark", self.asha.pk, data={"text": "Keep going!"}, follow=True)
        self.assertContains(response, "Remark saved")
        self.assertEqual(ReportRemark.objects.get(student=self.asha).text, "Keep going!")
        self.assertContains(self.get(self.u_t1, "marks_report", self.asha.pk), "Save remark")  # the form is offered
        self.assertContains(self.get(self.u_p1, "marks_report", self.asha.pk), "Keep going!")

    def test_nobody_else_may_write_a_remark(self):
        for user in (self.u_p1, self.u_student, self.u_t2):  # a subject teacher of 5B has no say about a 5A student
            response = self.post(user, "marks_remark", self.asha.pk, data={"text": "hacked"})
            self.assertIn(response.status_code, (403, 404), user.username)
        self.assertFalse(ReportRemark.objects.exists())
        self.assertNotContains(self.get(self.u_p1, "marks_report", self.asha.pk), "Save remark")

    def test_an_overlong_remark_is_refused_with_a_message(self):
        response = self.post(self.u_t1, "marks_remark", self.asha.pk, data={"text": "x" * 301}, follow=True)
        self.assertContains(response, "300 characters")
        self.assertFalse(ReportRemark.objects.exists())

    def test_remark_url_refuses_get(self):
        self.assertEqual(self.status(self.u_t1, "marks_remark", self.asha.pk), 405)


class ExamManagementScreenTests(MarksScreenCase):
    def test_only_the_principal_manages_exams(self):
        self.assertEqual(self.status(self.u_admin, "marks_exams"), 200)
        for user in (self.u_t1, self.u_office, self.u_accountant, self.u_p1, self.u_student):
            self.assertEqual(self.status(user, "marks_exams"), 403, user.username)
            self.assertEqual(self.post(user, "marks_exam_action", self.ut.pk, "publish").status_code, 403, user.username)
        self.assertFalse(Exam.objects.get(pk=self.ut.pk).is_published)

    def test_listing(self):
        response = self.get(self.u_admin, "marks_exams")
        self.assertEqual([r["exam"].name for r in response.context["rows"]], ["Unit Test", "Final"])
        self.assertEqual(response.context["total_weight"], D("100"))
        self.assertNotContains(response, "not 100%")
        Exam.objects.filter(pk=self.final.pk).update(weightage=D("70"))
        self.assertContains(self.get(self.u_admin, "marks_exams"), "not 100%")

    def test_publish_hide_and_lock(self):
        self.fill_asha()
        self.assertContains(self.post(self.u_admin, "marks_exam_action", self.ut.pk, "publish", follow=True), "now published")
        self.assertTrue(Exam.objects.get(pk=self.ut.pk).is_published)
        self.assertEqual(self.get(self.u_p1, "marks_home").context["kids"][0].latest["exam"], "Unit Test")  # parents see it now
        self.assertContains(self.post(self.u_admin, "marks_exam_action", self.ut.pk, "unpublish", follow=True), "hidden from parents")
        self.assertIsNone(self.get(self.u_p1, "marks_home").context["kids"][0].latest)
        self.post(self.u_admin, "marks_exam_action", self.ut.pk, "publish")
        self.assertContains(self.post(self.u_admin, "marks_exam_action", self.ut.pk, "lock", follow=True), "is locked")
        self.assertContains(self.post(self.u_admin, "marks_exam_action", self.ut.pk, "unpublish", follow=True), "locked")
        self.assertTrue(Exam.objects.get(pk=self.ut.pk).is_published)

    def test_cannot_lock_before_publishing(self):
        self.assertContains(self.post(self.u_admin, "marks_exam_action", self.ut.pk, "lock", follow=True), "Publish the exam before locking")
        self.assertFalse(Exam.objects.get(pk=self.ut.pk).is_locked)

    def test_creating_missing_papers(self):
        ExamPaper.objects.filter(exam=self.ut, grade=self.grade6).delete()
        response = self.post(self.u_admin, "marks_exam_action", self.ut.pk, "papers", data={"max_marks": "40"}, follow=True)
        self.assertContains(response, "Created 1 paper")
        self.assertEqual(ExamPaper.objects.get(exam=self.ut, grade=self.grade6).max_marks, D("40.00"))
        for bad in ("", "abc", "0", "AB", "-5"):
            self.assertContains(self.post(self.u_admin, "marks_exam_action", self.ut.pk, "papers", data={"max_marks": bad}, follow=True),
                                "maximum marks" if bad in ("", "0", "AB") else ("negative" if bad == "-5" else "number"), msg_prefix=bad)

    def test_unknown_actions_and_get_requests(self):
        self.assertEqual(self.post(self.u_admin, "marks_exam_action", self.ut.pk, "explode").status_code, 404)
        self.assertEqual(self.status(self.u_admin, "marks_exam_action", self.ut.pk, "publish"), 405)

    def test_another_schools_exam_does_not_exist_for_you(self):
        self.assertEqual(self.post(self.u_admin_b, "marks_exam_action", self.ut.pk, "publish").status_code, 404)
        self.assertFalse(Exam.objects.get(pk=self.ut.pk).is_published)


class AdminSiteTests(MarksScreenCase):
    def test_marks_are_read_only_in_the_admin_site(self):
        self.put(self.ut, self.math, self.asha, D("20"))
        self.client.force_login(self.u_admin)
        self.assertEqual(self.client.get(reverse("admin:assessments_mark_add")).status_code, 403)
        mark = Mark.objects.get()
        self.assertEqual(self.client.post(reverse("admin:assessments_mark_delete", args=[mark.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:assessments_mark_changelist")).status_code, 200)

    def test_principal_defines_exams_but_cannot_publish_through_the_admin(self):
        self.client.force_login(self.u_admin)
        response = self.client.post(
            reverse("admin:assessments_exam_add"),
            {"academic_year": self.year_a.pk, "name": "Pre-Board", "order": "5", "weightage": "10", "school": self.school_b.pk,
             "is_published": "on", "is_locked": "on"},
        )
        self.assertEqual(response.status_code, 302)
        exam = Exam.objects.get(name="Pre-Board")
        self.assertEqual((exam.school, exam.is_published, exam.is_locked), (self.school_a, False, False))  # flags are not settable here

    def test_teachers_and_office_cannot_change_exams(self):
        for user in (self.u_office,):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("admin:assessments_exam_add")).status_code, 403)
