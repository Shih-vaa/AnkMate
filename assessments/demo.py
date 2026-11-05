"""Fake exam results for the demo school (never run against real data)."""
import random
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from academics.models import AcademicYear, Enrollment, TeachingAssignment
from core.presets.icse import PRESET

from . import services
from .models import Exam, ExamPaper, Mark, ReportRemark
from .presets import apply_assessment_preset

# Which exams have results: (name, share of class-subject pairs a teacher has entered, published?)
SCHEDULE = [("Unit Test 1", 1.0, True), ("Half-Yearly", 1.0, True), ("Unit Test 2", 0.55, False)]

REMARKS = [
    (85, "Excellent work this session. Keep it up!"),
    (70, "Good progress. Steady revision will lift the weaker subjects."),
    (50, "Satisfactory. Needs more regular practice in the weaker subjects."),
    (0, "Needs extra support and regular practice. Please meet the class teacher."),
]


def clear_marks(school):
    Mark.objects.filter(school=school).delete()
    ReportRemark.objects.filter(school=school).delete()
    Exam.objects.filter(school=school).update(is_published=False, published_at=None, is_locked=False)


def seed_marks(school, *, seed=5, reset=False):
    """Create the exam structure if needed, then results. Returns {'marks': n, 'remarks': n}."""
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    if year is None:
        raise ValueError("The school has no current academic year.")
    apply_assessment_preset(school, year, PRESET)  # no-op when the exams already exist
    if Mark.objects.filter(school=school).exists():
        if not reset:
            raise ValueError("Marks already exist. Pass reset=True to replace them.")
        clear_marks(school)

    rng = random.Random(seed)
    exams = {e.name: e for e in Exam.objects.filter(school=school, academic_year=year)}
    papers = defaultdict(list)  # exam id -> papers
    for paper in ExamPaper.objects.filter(school=school, exam__academic_year=year):
        papers[paper.exam_id].append(paper)
    teacher_user = {
        (a.section_id, a.subject_id): a.teacher.user_id
        for a in TeachingAssignment.objects.filter(school=school, section__academic_year=year).select_related("teacher")
    }
    enrolments = list(Enrollment.objects.filter(school=school, academic_year=year, student__status="active").order_by())
    ability = {e.student_id: rng.gauss(66, 13) for e in enrolments}

    rows = []
    for name, entered_share, _published in SCHEDULE:
        exam = exams[name]
        entered_pairs = {}
        for e in enrolments:
            for paper in papers[exam.pk]:
                if paper.grade_id != e.section.grade_id:
                    continue
                pair = (e.section_id, paper.subject_id)
                if pair not in entered_pairs:
                    entered_pairs[pair] = rng.random() < entered_share
                if not entered_pairs[pair]:
                    continue
                if rng.random() < 0.015:
                    rows.append(Mark(school=school, paper=paper, student_id=e.student_id, absent=True,
                                     entered_by_id=teacher_user.get(pair)))
                    continue
                percent = min(100, max(5, ability[e.student_id] + rng.gauss(0, 7) + rng.gauss(0, 5)))
                marks = (Decimal(str(percent)) / 100 * paper.max_marks * 2).quantize(Decimal("1")) / 2
                rows.append(Mark(school=school, paper=paper, student_id=e.student_id, marks=min(marks, paper.max_marks),
                                 entered_by_id=teacher_user.get(pair)))
    Mark.objects.bulk_create(rows, batch_size=1000)

    for name, _share, published in SCHEDULE:
        if published:
            services.set_published(exams[name], True)

    # class teachers write a remark for about a third of their students
    remarks = 0
    for e in Enrollment.objects.filter(school=school, academic_year=year, student__status="active").select_related("student", "section__class_teacher"):
        if rng.random() > 0.35 or not e.section.class_teacher or not e.section.class_teacher.user_id:
            continue
        report = services.build_report(e.student, year, school, include_unpublished=False)
        if not report["overall"]:
            continue
        text = next(t for floor, t in REMARKS if report["overall"]["percent"] >= floor)
        ReportRemark.objects.create(school=school, student=e.student, academic_year=year, text=text,
                                    written_by_id=e.section.class_teacher.user_id, written_at=timezone.now())
        remarks += 1
    return {"marks": len(rows), "remarks": remarks}
