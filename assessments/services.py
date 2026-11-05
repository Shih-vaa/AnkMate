"""Marks rules: who may enter, how results are worked out, when parents may see them.

Per-school setting (School.settings):
    pass_percent    pass mark for a subject, percent                      default 33

HOW RESULTS ARE WORKED OUT
  * A subject's result is the *weighted* percentage over the exams that have a result for the student:
        sum(marks / max_marks * exam_weightage) / sum(exam_weightage) * 100
    Exams not held or not entered yet are left out, so mid-year figures are "so far", not final.
  * ABSENT counts as zero for that exam (it is a result); a blank (no row) is simply not entered yet.
  * Percentages are rounded to one decimal place first, and grades and pass/fail use that rounded figure, so what is
    printed always agrees with the decision made from it.
  * The overall percentage is the plain average of the subject percentages (optional subjects are shown but left out).
  * Parents and students only ever see PUBLISHED exams. Staff see drafts only for students in their own scope.
"""
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from academics.models import Enrollment, Grade, GradeSubject, Section, TeachingAssignment
from accounts.access import memberships_for, scopes_for, students_in_scope
from accounts.permissions import SCOPE_ALL, SCOPE_ASSIGNED
from people.models import Staff

from .models import Exam, ExamPaper, GradeBand, Mark, ReportRemark

ZERO = Decimal("0")
HUNDRED = Decimal("100")
TENTH = Decimal("0.1")
CENT = Decimal("0.01")
ABSENT_WORDS = {"ab", "a", "abs", "absent"}


class MarksError(ValueError):
    """A marks rule was broken; the message is fit to show to the person who triggered it."""


class MarksInputError(MarksError):
    """Invalid marks typed for particular students. `errors` maps student id -> message."""

    def __init__(self, errors):
        super().__init__("Some marks need fixing.")
        self.errors = errors


def today():
    """The school's 'today'. A seam: tests replace this to pin the date."""
    return timezone.localdate()


# -- numbers and grades ------------------------------------------------------------------------

def pct(value):
    """A percentage rounded to one decimal place, half-up."""
    return Decimal(value).quantize(TENTH, rounding=ROUND_HALF_UP)


def pass_percent(school):
    try:
        value = Decimal(str((school.settings or {}).get("pass_percent", 33)))
    except (InvalidOperation, ValueError):
        return Decimal(33)
    return min(HUNDRED, max(ZERO, value))


def grade_bands(school):
    return list(GradeBand.objects.filter(school=school).order_by("-min_percent"))


def grade_for(bands, percent):
    """The label of the highest band whose minimum the percentage reaches ('-' if there is no scale)."""
    if percent is None:
        return ""
    for band in bands:  # highest minimum first
        if percent >= band.min_percent:
            return band.label
    return "-"


# -- who may see / enter ---------------------------------------------------------------------

def _active_staff_ids(user, school):
    return list(Staff.objects.filter(school=school, user=user, is_active=True).values_list("pk", flat=True))


def _entry_memberships(user, school):
    return [m for m in memberships_for(user, school) if "marks.enter" in m.role.permissions]


def can_see_drafts(user, school, student):
    """May this user see UNPUBLISHED results of this student? Only staff whose scope includes the student.
    (A teacher who is also a parent never gets drafts of their own child that way.)"""
    scopes = scopes_for(user, school, "marks.view")
    if SCOPE_ALL in scopes:
        return True
    if SCOPE_ASSIGNED in scopes:
        return students_in_scope(user, school, SCOPE_ASSIGNED).filter(pk=student.pk).exists()
    return False


def teaching_pairs(user, school):
    """{(section_id, subject_id)} the user is assigned to teach in the current year."""
    staff = _active_staff_ids(user, school)
    return set(
        TeachingAssignment.objects.filter(school=school, teacher_id__in=staff, section__academic_year__is_current=True)
        .values_list("section_id", "subject_id")
    )


def paper_for(exam, section, subject):
    return ExamPaper.objects.filter(exam=exam, grade_id=section.grade_id, subject=subject).first()


def can_enter(user, school, exam, section, subject):
    """(allowed, reason). Teachers: only their own subject in their own class, and only before publication.
    School-wide staff (the principal): any paper, until the exam is locked."""
    memberships = _entry_memberships(user, school)
    if not memberships:
        return False, "You don't have permission to enter marks."
    if exam.school_id != school.id or section.school_id != school.id:
        return False, "That class or exam isn't part of this school."
    if section.academic_year_id != exam.academic_year_id:
        return False, "That class isn't part of this exam's academic year."
    if paper_for(exam, section, subject) is None:
        return False, "This exam has no paper for that subject and class."
    if exam.is_locked:
        return False, "This exam is locked, so marks can't be changed."
    if any(m.role.scope == SCOPE_ALL for m in memberships):
        return True, ""
    if exam.is_published:
        return False, "Results are already published. Please ask the principal to correct a mark."
    if (section.pk, subject.pk) not in teaching_pairs(user, school):
        return False, "You aren't assigned to teach this subject in this class."
    return True, ""


def roster(section):
    """Active students enrolled in the section, in roll-number order."""
    return list(
        Enrollment.objects.filter(section=section, student__status="active")
        .select_related("student").order_by("roll_no", "student__first_name")
    )


def entry_targets(user, school, exam):
    """What this user may enter for the exam: rows of (section, subject, paper, entered, total, editable)."""
    memberships = _entry_memberships(user, school)
    if not memberships:
        return []
    papers = {(p.grade_id, p.subject_id): p for p in exam.papers.select_related("subject")}
    has_all = any(m.role.scope == SCOPE_ALL for m in memberships)

    if has_all:
        sections = list(Section.objects.filter(school=school, academic_year=exam.academic_year).select_related("grade"))
        subjects_by_grade = defaultdict(list)
        for gs in GradeSubject.objects.filter(school=school).select_related("subject"):
            subjects_by_grade[gs.grade_id].append(gs.subject)
        pairs = [(s, subject) for s in sections for subject in subjects_by_grade[s.grade_id]]
    else:
        assignments = TeachingAssignment.objects.filter(
            school=school, teacher_id__in=_active_staff_ids(user, school), section__academic_year=exam.academic_year
        ).select_related("section__grade", "subject")
        pairs = [(a.section, a.subject) for a in assignments]

    pairs = [(s, subj) for s, subj in pairs if (s.grade_id, subj.pk) in papers]
    section_ids = {s.pk for s, _ in pairs}
    members = defaultdict(set)
    for section_id, student_id in Enrollment.objects.filter(section_id__in=section_ids, student__status="active").values_list("section_id", "student_id"):
        members[section_id].add(student_id)
    entered = defaultdict(set)
    for paper_id, student_id in Mark.objects.filter(paper__exam=exam).values_list("paper_id", "student_id"):
        entered[paper_id].add(student_id)

    locked_out = exam.is_locked or (exam.is_published and not has_all)
    rows = []
    for section, subject in pairs:
        paper = papers[(section.grade_id, subject.pk)]
        roster_ids = members[section.pk]
        rows.append(
            {
                "section": section, "subject": subject, "paper": paper, "total": len(roster_ids),
                "entered": len(roster_ids & entered[paper.pk]), "editable": not locked_out,
            }
        )
    rows.sort(key=lambda r: (r["section"].grade.order, r["section"].name, r["subject"].name))
    return rows


# -- entering marks ----------------------------------------------------------------------------

def parse_mark(text, max_marks):
    """Typed entry -> None (blank = not entered), 'AB' (absent) or a Decimal. Raises ValueError with a friendly message."""
    text = (text or "").strip()
    if not text:
        return None
    if text.lower() in ABSENT_WORDS:
        return "AB"
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("Enter a number, or AB for absent.")
    if not value.is_finite():
        raise ValueError("Enter a number, or AB for absent.")
    if value < 0:
        raise ValueError("Marks can't be negative.")
    if value != value.quantize(CENT):
        raise ValueError("Use at most two decimal places.")
    if value > max_marks:
        raise ValueError(f"Can't be more than the maximum of {max_marks.normalize():f}.")
    return value.quantize(CENT)


@transaction.atomic
def save_marks(paper, section, entries, user, *, now=None):
    """Create, change or clear marks. `entries` = {student_id: Decimal | 'AB' | None}; None clears the mark.
    Returns {'saved': n, 'cleared': n}. Raises MarksInputError (per-student messages) or MarksError."""
    allowed = {e.student_id for e in roster(section)}
    if set(entries) - allowed:
        raise MarksError("Some students aren't in this class.")
    errors = {}
    for sid, value in entries.items():
        if isinstance(value, Decimal) and not (ZERO <= value <= paper.max_marks):
            errors[sid] = f"Must be between 0 and {paper.max_marks.normalize():f}."
        elif value is not None and value != "AB" and not isinstance(value, Decimal):
            errors[sid] = "Enter a number, or AB for absent."
    if errors:
        raise MarksInputError(errors)

    now = now or timezone.now()
    to_clear = [sid for sid, value in entries.items() if value is None]
    removed = Mark.objects.filter(paper=paper, student_id__in=to_clear).delete()[1].get("assessments.Mark", 0)
    rows = [
        Mark(
            school_id=paper.school_id, paper=paper, student_id=sid, absent=(value == "AB"),
            marks=None if value == "AB" else value, entered_by=user, entered_at=now,
        )
        for sid, value in entries.items() if value is not None
    ]
    Mark.objects.bulk_create(
        rows, update_conflicts=True, unique_fields=["paper", "student"],
        update_fields=["marks", "absent", "entered_by", "entered_at"],
    )
    return {"saved": len(rows), "cleared": removed}  # only marks that actually existed count as cleared


# -- a student's results and report card ---------------------------------------------------------

def build_report(student, year, school, *, include_unpublished=False):
    """Everything a report card or progress page needs, worked out in one place (see module docstring)."""
    enrolment = Enrollment.objects.filter(student=student, academic_year=year).select_related("section__grade").first()
    bands, threshold = grade_bands(school), pass_percent(school)
    report = {
        "student": student, "year": year, "section": enrolment.section if enrolment else None, "exams": [], "subjects": [],
        "exam_totals": [], "overall": None, "bands": bands, "pass_percent": threshold, "has_draft": False,
    }
    if enrolment is None:
        return report

    exams = list(Exam.objects.filter(academic_year=year).order_by("order"))
    report["has_draft"] = include_unpublished and any(not e.is_published for e in exams)
    if not include_unpublished:
        exams = [e for e in exams if e.is_published]
    report["exams"] = exams
    papers = list(ExamPaper.objects.filter(exam__in=exams, grade=enrolment.section.grade).select_related("subject"))
    paper_map = {(p.exam_id, p.subject_id): p for p in papers}
    marks = {m.paper_id: m for m in Mark.objects.filter(student=student, paper__in=papers)}
    optional = set(
        GradeSubject.objects.filter(grade=enrolment.section.grade, is_optional=True).values_list("subject_id", flat=True)
    )
    subjects = sorted({p.subject for p in papers}, key=lambda s: s.name)

    totals = {e.pk: {"obtained": ZERO, "maximum": ZERO, "papers": 0, "results": 0} for e in exams}
    for subject in subjects:
        cells, numerator, denominator, with_paper, with_result = [], ZERO, ZERO, 0, 0
        for exam in exams:
            paper = paper_map.get((exam.pk, subject.pk))
            if paper is None:
                cells.append({"exam": exam, "state": "none"})
                continue
            with_paper += 1
            totals[exam.pk]["papers"] += 1
            mark = marks.get(paper.pk)
            if mark is None:
                cells.append({"exam": exam, "state": "pending", "max": paper.max_marks})
                continue
            with_result += 1
            totals[exam.pk]["results"] += 1
            totals[exam.pk]["maximum"] += paper.max_marks
            if mark.absent:
                cells.append({"exam": exam, "state": "absent", "max": paper.max_marks, "percent": pct(ZERO)})
                denominator += exam.weightage
            else:
                fraction = mark.marks / paper.max_marks
                cells.append(
                    {"exam": exam, "state": "result", "marks": mark.marks, "max": paper.max_marks, "percent": pct(fraction * HUNDRED)}
                )
                numerator += fraction * exam.weightage
                denominator += exam.weightage
                totals[exam.pk]["obtained"] += mark.marks
        percent = pct(numerator / denominator * HUNDRED) if denominator > 0 else None
        report["subjects"].append(
            {
                "subject": subject, "optional": subject.pk in optional, "cells": cells, "percent": percent,
                "grade": grade_for(bands, percent), "passed": None if percent is None else percent >= threshold,
                "complete": with_paper > 0 and with_result == with_paper,
            }
        )

    for exam in exams:
        t = totals[exam.pk]
        percent = pct(t["obtained"] / t["maximum"] * HUNDRED) if t["maximum"] > 0 else None
        report["exam_totals"].append(
            {"exam": exam, "obtained": t["obtained"], "maximum": t["maximum"], "percent": percent,
             "grade": grade_for(bands, percent), "complete": t["papers"] > 0 and t["results"] == t["papers"]}
        )

    scored = [s for s in report["subjects"] if s["percent"] is not None]
    counted = [s for s in scored if not s["optional"]] or scored
    if counted:
        overall = pct(sum((s["percent"] for s in counted), ZERO) / len(counted))
        below = [s["subject"].name for s in counted if not s["passed"]]
        report["overall"] = {
            "percent": overall, "grade": grade_for(bands, overall), "below_pass": below,
            "complete": all(s["complete"] for s in report["subjects"]) and bool(exams),
        }
    return report


def latest_results_for(student_ids, year, school):
    """{student_id: {'exam', 'percent', 'grade'}} for each child's most recent PUBLISHED exam (one query)."""
    bands = grade_bands(school)
    rows = (
        Mark.objects.filter(student_id__in=list(student_ids), paper__exam__academic_year=year, paper__exam__is_published=True)
        .order_by()
        .values("student_id", "paper__exam__order", "paper__exam__name")
        .annotate(obtained=Sum("marks"), maximum=Sum("paper__max_marks"))
    )
    latest = {}
    for row in rows:
        sid = row["student_id"]
        if sid not in latest or row["paper__exam__order"] > latest[sid]["order"]:
            percent = pct((row["obtained"] or ZERO) / row["maximum"] * HUNDRED) if row["maximum"] else None
            latest[sid] = {"order": row["paper__exam__order"], "exam": row["paper__exam__name"], "percent": percent,
                           "grade": grade_for(bands, percent)}
    return latest


# -- class marksheet and analysis -----------------------------------------------------------------

def build_marksheet(section, exam, school):
    """Students x subjects for one exam in one class, with each student's total and each subject's analysis."""
    bands, threshold = grade_bands(school), pass_percent(school)
    enrolments = roster(section)
    papers = list(ExamPaper.objects.filter(exam=exam, grade=section.grade).select_related("subject").order_by("subject__name"))
    marks = {
        (m.paper_id, m.student_id): m
        for m in Mark.objects.filter(paper__in=papers, student_id__in=[e.student_id for e in enrolments])
    }
    analysis = {p.pk: {"paper": p, "entered": 0, "absent": 0, "percents": [], "passed": 0, "grades": Counter()} for p in papers}
    rows = []
    for e in enrolments:
        cells, obtained, maximum, entered = [], ZERO, ZERO, 0
        for paper in papers:
            mark = marks.get((paper.pk, e.student_id))
            stats = analysis[paper.pk]
            if mark is None:
                cells.append({"paper": paper, "state": "pending"})
                continue
            entered += 1
            stats["entered"] += 1
            maximum += paper.max_marks
            if mark.absent:
                stats["absent"] += 1
                cells.append({"paper": paper, "state": "absent"})
                continue
            percent = pct(mark.marks / paper.max_marks * HUNDRED)
            obtained += mark.marks
            stats["percents"].append(percent)
            stats["passed"] += 1 if percent >= threshold else 0
            stats["grades"][grade_for(bands, percent)] += 1
            cells.append({"paper": paper, "state": "result", "marks": mark.marks, "percent": percent,
                          "failed": percent < threshold})
        percent = pct(obtained / maximum * HUNDRED) if maximum > 0 else None
        rows.append(
            {"student": e.student, "roll_no": e.roll_no, "cells": cells, "obtained": obtained, "maximum": maximum,
             "percent": percent, "grade": grade_for(bands, percent), "entered": entered, "complete": entered == len(papers) and bool(papers)}
        )
    subject_stats = []
    for paper in papers:
        stats = analysis[paper.pk]
        percents = stats["percents"]
        subject_stats.append(
            {
                "paper": paper, "entered": stats["entered"], "absent": stats["absent"],
                "average": pct(sum(percents, ZERO) / len(percents)) if percents else None,
                "highest": max(percents) if percents else None, "lowest": min(percents) if percents else None,
                "pass_rate": pct(Decimal(stats["passed"]) / len(percents) * HUNDRED) if percents else None,
                "grades": [(b.label, stats["grades"].get(b.label, 0)) for b in bands if stats["grades"].get(b.label, 0)],
            }
        )
    needs_attention = [r for r in rows if r["percent"] is not None and r["percent"] < threshold]
    return {"rows": rows, "papers": papers, "subject_stats": subject_stats, "needs_attention": needs_attention,
            "pass_percent": threshold, "students": len(rows)}


# -- exam management ---------------------------------------------------------------------------

def create_papers(exam, max_marks):
    """Give every subject of every class a paper in this exam (existing papers are left alone). Returns how many were created."""
    created = 0
    for gs in GradeSubject.objects.filter(school=exam.school).select_related("grade", "subject"):
        _, was_created = ExamPaper.objects.get_or_create(
            school=exam.school, exam=exam, grade=gs.grade, subject=gs.subject, defaults={"max_marks": Decimal(max_marks)}
        )
        created += was_created
    return created


def set_published(exam, publish):
    if exam.is_locked:
        raise MarksError("A locked exam can't be changed.")
    exam.is_published = bool(publish)
    exam.published_at = timezone.now() if publish else None
    exam.save(update_fields=["is_published", "published_at"])
    return exam


def lock_exam(exam):
    if not exam.is_published:
        raise MarksError("Publish the exam before locking it.")
    exam.is_locked = True
    exam.save(update_fields=["is_locked"])
    return exam


def exam_progress(exam):
    """How complete marks entry is: {'papers', 'expected', 'entered'}."""
    students_per_grade = dict(
        Enrollment.objects.filter(academic_year=exam.academic_year, student__status="active")
        .order_by().values_list("section__grade_id").annotate(n=Count("id"))
    )
    papers_per_grade = dict(exam.papers.order_by().values_list("grade_id").annotate(n=Count("id")))
    expected = sum(students_per_grade.get(g, 0) * n for g, n in papers_per_grade.items())
    return {"papers": sum(papers_per_grade.values()), "expected": expected, "entered": Mark.objects.filter(paper__exam=exam).count()}


# -- report-card remarks -------------------------------------------------------------------------

def can_write_remark(user, school, student, year):
    """The class teacher of the student's section, or school-wide staff with marks-entry rights."""
    memberships = _entry_memberships(user, school)
    if any(m.role.scope == SCOPE_ALL for m in memberships):
        return True
    if any(m.role.scope == SCOPE_ASSIGNED for m in memberships):
        staff = _active_staff_ids(user, school)
        return Enrollment.objects.filter(student=student, academic_year=year, section__class_teacher_id__in=staff).exists()
    return False


def save_remark(student, year, text, user):
    text = (text or "").strip()
    if len(text) > 300:
        raise MarksError("Please keep the remark to 300 characters.")
    if not text:
        ReportRemark.objects.filter(student=student, academic_year=year).delete()
        return None
    remark, _ = ReportRemark.objects.update_or_create(
        school=student.school, student=student, academic_year=year,
        defaults={"text": text, "written_by": user, "written_at": timezone.now()},
    )
    return remark
