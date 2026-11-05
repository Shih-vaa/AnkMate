from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from academics.models import AcademicYear, Section, Subject
from academics.queries import with_current_class
from accounts.access import has_perm, scopes_for, students_in_scope, visible_sections, visible_students
from accounts.decorators import require_any_perm, require_perm
from accounts.permissions import SCOPE_ALL, SCOPE_CHILDREN, SCOPE_SELF
from attendance import services as attendance_services
from people.models import Student

from . import services
from .models import Exam, ReportRemark


def _current_year(school):
    return AcademicYear.objects.filter(school=school, is_current=True).first()


def _plain(value):
    """Decimal -> '18.5' / '20' for input boxes (no trailing zeros, no exponent)."""
    return format(value.normalize(), "f")


# -- home --------------------------------------------------------------------------------------

@require_any_perm("marks.view", "marks.enter")
def home(request):
    school, user = request.school, request.user
    year = _current_year(school)
    scopes = scopes_for(user, school, "marks.view")
    ctx = {"year": year}

    staff = bool(scopes & {SCOPE_ALL, "assigned"})
    can_enter = has_perm(user, school, "marks.enter")
    if (staff or can_enter) and year:
        exams = list(Exam.objects.filter(school=school, academic_year=year).order_by("order"))
        is_principal = has_perm(user, school, "academics.manage", all_scope=True)
        for exam in exams:
            exam.progress = services.exam_progress(exam) if is_principal else None
        ctx.update(exams=exams, can_enter=can_enter, can_view_sheets=staff, is_principal=is_principal)

    kid_scopes = scopes & {SCOPE_CHILDREN, SCOPE_SELF}
    if kid_scopes and year:
        ids = set()
        for scope in kid_scopes:
            ids |= set(students_in_scope(user, school, scope).values_list("pk", flat=True))
        kids = list(with_current_class(Student.objects.filter(pk__in=ids)).order_by("first_name"))
        latest = services.latest_results_for(ids, year, school)
        for kid in kids:
            kid.latest = latest.get(kid.pk)
        ctx["kids"] = kids
    return render(request, "assessments/home.html", ctx)


# -- entering marks ----------------------------------------------------------------------------

@require_perm("marks.enter")
def entry_index(request, exam_pk):
    exam = get_object_or_404(Exam.objects.select_related("academic_year"), pk=exam_pk, school=request.school)
    return render(
        request, "assessments/entry_index.html",
        {"exam": exam, "targets": services.entry_targets(request.user, request.school, exam)},
    )


@require_perm("marks.enter")
def enter(request, exam_pk, section_pk, subject_pk):
    school, user = request.school, request.user
    exam = get_object_or_404(Exam, pk=exam_pk, school=school)
    section = get_object_or_404(Section.objects.select_related("grade", "academic_year"), pk=section_pk, school=school)
    subject = get_object_or_404(Subject, pk=subject_pk, school=school)
    paper = services.paper_for(exam, section, subject)
    if paper is None:
        raise Http404
    # A class/subject that isn't yours simply doesn't exist for you.
    if SCOPE_ALL not in scopes_for(user, school, "marks.enter") and (section.pk, subject.pk) not in services.teaching_pairs(user, school):
        raise Http404

    editable, why_not = services.can_enter(user, school, exam, section, subject)
    students = services.roster(section)
    here = reverse("marks_enter", args=[exam.pk, section.pk, subject.pk])
    existing = {m.student_id: m for m in paper.marks.filter(student_id__in=[e.student_id for e in students])}

    posted, errors = {}, {}
    if request.method == "POST":
        if not editable:
            messages.error(request, why_not)
            return redirect(here)
        entries = {}
        for e in students:
            sid = e.student_id
            text = request.POST.get(f"m_{sid}", "")
            absent = request.POST.get(f"ab_{sid}") == "on"
            posted[sid] = {"value": text, "absent": absent}
            if absent:
                entries[sid] = "AB"
                continue
            try:
                entries[sid] = services.parse_mark(text, paper.max_marks)
            except ValueError as error:
                errors[sid] = str(error)
        if not errors:
            try:
                result = services.save_marks(paper, section, entries, user)
            except services.MarksInputError as error:
                errors = error.errors
            except services.MarksError as error:
                messages.error(request, str(error))
                return redirect(here)
            else:
                messages.success(
                    request,
                    f"Saved {result['saved']} mark{'s' if result['saved'] != 1 else ''} for {subject.name}, {section}"
                    + (f" ({result['cleared']} cleared)." if result["cleared"] else "."),
                )
                return redirect(here)
        messages.error(request, "Some marks need fixing. Nothing was saved yet.")

    rows = []
    for e in students:
        mark = existing.get(e.student_id)
        if e.student_id in posted:
            value, absent = posted[e.student_id]["value"], posted[e.student_id]["absent"]
        else:
            absent = bool(mark and mark.absent)
            value = _plain(mark.marks) if mark and mark.marks is not None else ""
        rows.append({"student": e.student, "roll_no": e.roll_no, "value": value, "absent": absent, "error": errors.get(e.student_id, "")})
    return render(
        request, "assessments/enter.html",
        {"exam": exam, "section": section, "subject": subject, "paper": paper, "rows": rows,
         "editable": editable, "blocked_reason": "" if editable else why_not,
         "entered": sum(1 for m in existing.values()), "total": len(students)},
    )


# -- class marksheets --------------------------------------------------------------------------

@require_perm("marks.view")
def sheet_index(request, exam_pk):
    school, user = request.school, request.user
    exam = get_object_or_404(Exam, pk=exam_pk, school=school)
    sections = list(visible_sections(user, school, "marks.view").select_related("grade").order_by("grade__order", "name"))
    if not sections:
        raise Http404
    return render(request, "assessments/sheet_index.html", {"exam": exam, "sections": sections})


@require_perm("marks.view")
def sheet(request, exam_pk, section_pk):
    school, user = request.school, request.user
    exam = get_object_or_404(Exam, pk=exam_pk, school=school)
    section = get_object_or_404(visible_sections(user, school, "marks.view").select_related("grade", "class_teacher"), pk=section_pk)
    return render(
        request, "assessments/sheet.html",
        {"exam": exam, "section": section, "sheet": services.build_marksheet(section, exam, school)},
    )


# -- one student: progress and report card -------------------------------------------------------

def _student_and_report(request, pk):
    """The student (only if in the user's scope), whether the user may see drafts for them, and their results."""
    school, user = request.school, request.user
    student = get_object_or_404(with_current_class(visible_students(user, school, "marks.view")), pk=pk)
    year = _current_year(school)
    drafts = services.can_see_drafts(user, school, student)
    report = services.build_report(student, year, school, include_unpublished=drafts) if year else None
    return student, drafts, report


@require_perm("marks.view")
def student_progress(request, pk):
    student, drafts, report = _student_and_report(request, pk)
    return render(request, "assessments/student.html", {"student": student, "report": report, "drafts": drafts})


@require_perm("marks.view")
def report_card(request, pk):
    school, user = request.school, request.user
    student = get_object_or_404(with_current_class(visible_students(user, school, "marks.view")), pk=pk)
    year = _current_year(school)
    drafts = services.can_see_drafts(user, school, student)
    preview = drafts and request.GET.get("preview") == "1"  # staff may preview unpublished marks; never parents
    report = services.build_report(student, year, school, include_unpublished=preview) if year else None
    attendance = None
    if year:
        attendance = attendance_services.percentages_for([student.pk], year.start_date, attendance_services.today())[student.pk]
    remark = ReportRemark.objects.filter(student=student, academic_year=year).first() if year else None
    return render(
        request, "assessments/report.html",
        {"student": student, "report": report, "attendance": attendance, "remark": remark, "preview": preview, "can_preview": drafts,
         "can_remark": bool(year) and services.can_write_remark(user, school, student, year)},
    )


@require_perm("marks.enter")
@require_POST
def remark_save(request, pk):
    school, user = request.school, request.user
    student = get_object_or_404(visible_students(user, school, "marks.view"), pk=pk)
    year = _current_year(school)
    if year is None or not services.can_write_remark(user, school, student, year):
        raise PermissionDenied
    try:
        services.save_remark(student, year, request.POST.get("text", ""), user)
    except services.MarksError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Remark saved.")
    return redirect("marks_report", pk=student.pk)


# -- principal: exam setup, publishing, locking ----------------------------------------------------

@require_perm("academics.manage", all_scope=True)
def exams(request):
    school = request.school
    year = _current_year(school)
    rows = []
    if year:
        for exam in Exam.objects.filter(school=school, academic_year=year).order_by("order"):
            rows.append({"exam": exam, "progress": services.exam_progress(exam)})
    total_weight = sum((r["exam"].weightage for r in rows), Decimal("0"))
    return render(request, "assessments/exams.html", {"year": year, "rows": rows, "total_weight": total_weight})


@require_perm("academics.manage", all_scope=True)
@require_POST
def exam_action(request, pk, action):
    exam = get_object_or_404(Exam, pk=pk, school=request.school)
    try:
        if action == "publish":
            services.set_published(exam, True)
            messages.success(request, f"{exam.name} is now published. Parents and students can see the results.")
        elif action == "unpublish":
            services.set_published(exam, False)
            messages.success(request, f"{exam.name} is hidden from parents and students again.")
        elif action == "lock":
            services.lock_exam(exam)
            messages.success(request, f"{exam.name} is locked. Its marks can no longer be changed.")
        elif action == "papers":
            try:
                max_marks = services.parse_mark(request.POST.get("max_marks", ""), Decimal("1000"))
            except ValueError as error:
                raise services.MarksError(str(error))
            if not isinstance(max_marks, Decimal) or max_marks <= 0:
                raise services.MarksError("Enter the maximum marks, for example 100.")
            created = services.create_papers(exam, max_marks)
            messages.success(request, f"Created {created} paper{'s' if created != 1 else ''} for {exam.name}.")
        else:
            raise Http404
    except services.MarksError as error:
        messages.error(request, str(error))
    return redirect("marks_exams")
