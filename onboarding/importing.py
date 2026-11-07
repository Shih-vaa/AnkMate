"""Importing students (with their parents) and staff from a CSV file.

Two steps, so nothing surprising ever happens to the school's data:
  1. read_and_check_*: read the file, check EVERY row, and stage the result (nothing is created);
  2. apply_*: create exactly what was previewed, in one transaction.

RULES
  * Existing records are never overwritten: an admission number or employee code that already exists is skipped.
  * Rows with problems are skipped and reported (with a downloadable file of just those rows to fix and re-upload);
    good rows are still imported if the office chooses.
  * Parents are matched by phone number, so brothers and sisters share one parent record, and so do repeat imports.
  * The file is data, never instructions: values are stored as plain text and length-checked.
"""
import csv
import io
import re
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from academics.models import AcademicYear, Enrollment, Grade, Section
from accounts.models import normalize_phone
from people.models import Gender, Guardian, Staff, Student, StudentGuardian

from .models import ImportBatch

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000

STUDENT_COLUMNS = [
    "admission_no", "first_name", "last_name", "gender", "date_of_birth", "admission_date", "class", "section", "roll_no",
    "address", "guardian_name", "guardian_phone", "guardian_relation", "guardian_email",
    "guardian2_name", "guardian2_phone", "guardian2_relation",
]
STUDENT_REQUIRED = ["admission_no", "first_name", "class", "section"]
STAFF_COLUMNS = ["employee_code", "first_name", "last_name", "gender", "phone", "email", "designation", "date_joined"]
STAFF_REQUIRED = ["employee_code", "first_name"]

# Column headings people actually use in their spreadsheets -> our names (headings are lower-cased, punctuation removed).
STUDENT_SYNONYMS = {
    "admission_number": "admission_no", "adm_no": "admission_no", "admno": "admission_no", "admission_id": "admission_no",
    "firstname": "first_name", "given_name": "first_name", "student_first_name": "first_name",
    "lastname": "last_name", "surname": "last_name", "family_name": "last_name",
    "sex": "gender",
    "dob": "date_of_birth", "d_o_b": "date_of_birth", "birth_date": "date_of_birth",
    "date_of_admission": "admission_date", "doa": "admission_date",
    "grade": "class", "standard": "class", "std": "class", "class_name": "class",
    "sec": "section", "section_name": "section", "div": "section", "division": "section",
    "roll": "roll_no", "roll_number": "roll_no", "rollno": "roll_no",
    "residential_address": "address",
    "parent_name": "guardian_name", "parent": "guardian_name", "guardian": "guardian_name",
    "parent_phone": "guardian_phone", "parent_mobile": "guardian_phone", "guardian_mobile": "guardian_phone", "mobile": "guardian_phone",
    "mobile_no": "guardian_phone", "phone": "guardian_phone", "phone_no": "guardian_phone", "contact": "guardian_phone",
    "contact_no": "guardian_phone", "parent_contact": "guardian_phone",
    "relation": "guardian_relation", "relationship": "guardian_relation", "parent_relation": "guardian_relation",
    "parent_email": "guardian_email", "email": "guardian_email",
    "guardian_2_name": "guardian2_name", "guardian_2_phone": "guardian2_phone", "guardian_2_relation": "guardian2_relation",
}
STAFF_SYNONYMS = {
    "emp_code": "employee_code", "employee_id": "employee_code", "staff_id": "employee_code", "code": "employee_code",
    "firstname": "first_name", "lastname": "last_name", "surname": "last_name", "sex": "gender",
    "mobile": "phone", "mobile_no": "phone", "contact": "phone", "contact_no": "phone", "phone_no": "phone",
    "email_id": "email", "position": "designation", "post": "designation", "job_title": "designation",
    "joining_date": "date_joined", "doj": "date_joined", "date_of_joining": "date_joined",
}

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y"]  # day first, as in India
GENDERS = {"m": "M", "male": "M", "boy": "M", "f": "F", "female": "F", "girl": "F", "o": "O", "other": "O", "others": "O"}
RELATIONS = {"father": "father", "dad": "father", "f": "father", "mother": "mother", "mom": "mother", "mum": "mother", "m": "mother",
             "guardian": "guardian", "other": "guardian", "": "guardian"}
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}


class ImportFileError(ValueError):
    """The file itself can't be used (wrong format, missing columns, too big). The message is fit to show."""


def today():
    return timezone.localdate()


# -- reading the file -----------------------------------------------------------------------------

def _heading(text):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def read_csv(raw, columns, required, synonyms):
    """bytes -> (records, ignored_headings). Each record is {'row': file line number, 'values': {column: text}}."""
    if len(raw) > MAX_BYTES:
        raise ImportFileError("That file is larger than 2 MB. Please split it into smaller files.")
    for encoding in ("utf-8-sig", "cp1252"):  # Excel's "CSV UTF-8" first, then older Excel exports
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportFileError("That file isn't a text/CSV file. In Excel use Save As > CSV UTF-8.")
    first_line = text.split("\n", 1)[0]
    delimiter = max((",", ";", "\t"), key=first_line.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows or not any(cell.strip() for cell in rows[0]):
        raise ImportFileError("The file is empty, or its first row doesn't contain column headings.")

    mapping, ignored = {}, []
    for index, heading in enumerate(rows[0]):
        name = _heading(heading)
        name = synonyms.get(name, name)
        if name in columns and name not in mapping.values():
            mapping[index] = name
        elif heading.strip():
            ignored.append(heading.strip())
    missing = [c for c in required if c not in mapping.values()]
    if missing:
        raise ImportFileError(
            f"The file is missing the column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}. "
            f"Download the template to see the expected columns."
        )
    if len(rows) - 1 > MAX_ROWS:
        raise ImportFileError(f"That file has more than {MAX_ROWS} rows. Please split it into smaller files.")

    records = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        records.append({"row": line_no, "values": {name: (row[i].strip() if i < len(row) else "") for i, name in mapping.items()}})
    return records, ignored


# -- cleaning single values -------------------------------------------------------------------------------

def parse_date(text):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("Use a date like 2015-04-23 or 23/04/2015.")


def class_key(text):
    """'Class V', '5', '5th', 'std 5' all become '5'; 'Nursery' stays 'nursery'."""
    t = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    t = re.sub(r"\b(class|std|standard|grade)\b", " ", t)
    t = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", " ".join(t.split()))
    t = " ".join(t.split())
    return str(ROMAN[t]) if t in ROMAN else t.replace(" ", "")


def split_name(full):
    full = " ".join((full or "").split())
    if " " not in full:
        return full, ""
    first, last = full.rsplit(" ", 1)
    return first, last


class _Checker:
    """Collects the problems and warnings of one row while its values are cleaned."""

    def __init__(self, values):
        self.values, self.errors, self.warnings = values, [], []

    def text(self, key, label, *, required=False, max_len=80):
        value = " ".join(self.values.get(key, "").split())
        if required and not value:
            self.errors.append(f"{label} is required.")
        elif len(value) > max_len:
            self.errors.append(f"{label} is too long (more than {max_len} characters).")
            return ""
        return value

    def date(self, key, label, *, sanity=None):
        value = self.values.get(key, "")
        if not value:
            return None
        try:
            parsed = parse_date(value)
        except ValueError as error:
            self.errors.append(f"{label}: {error}")
            return None
        if sanity and not sanity[0] <= parsed <= sanity[1]:
            self.errors.append(f"{label} {parsed:%d %b %Y} looks wrong.")
            return None
        return parsed

    def gender(self):
        value = self.values.get("gender", "").lower()
        if not value:
            return ""
        if value not in GENDERS:
            self.errors.append("Gender should be Male, Female or Other.")
            return ""
        return GENDERS[value]

    def phone(self, value, label, *, required=False):
        if not value:
            if required:
                self.errors.append(f"{label} is required.")
            return ""
        digits = normalize_phone(value)
        if len(digits) != 10:
            self.errors.append(f"{label} '{value}' should be a 10-digit number.")
            return ""
        if digits[0] not in "6789":
            self.warnings.append(f"{label} {digits} doesn't look like a mobile number.")
        return digits


def _iso(value):
    return value.isoformat() if value else None


# -- students ---------------------------------------------------------------------------------------------

def check_students(school, records):
    """Validate every student row. Returns (good, problems, skipped, summary); nothing is written."""
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    if year is None:
        raise ImportFileError("This school has no current academic year yet, so students can't be placed in classes.")
    grades = {class_key(g.name): g for g in Grade.objects.filter(school=school)}
    sections = {(s.grade_id, s.name.casefold()): s for s in Section.objects.filter(school=school, academic_year=year)}
    sections_of = {}
    for s in sections.values():
        sections_of.setdefault(s.grade_id, []).append(s.name)
    existing = {a.casefold() for a in Student.objects.filter(school=school).values_list("admission_no", flat=True)}
    known_guardians = {}
    for g in Guardian.objects.filter(school=school).exclude(phone="").order_by("pk"):
        known_guardians.setdefault(normalize_phone(g.phone), g)
    taken_rolls = {}
    for section_id, roll in Enrollment.objects.filter(school=school, academic_year=year, roll_no__isnull=False).values_list("section_id", "roll_no"):
        taken_rolls.setdefault(section_id, set()).add(roll)

    now = today()
    dob_range = (date(now.year - 25, 1, 1), date(now.year - 2, 12, 31))
    seen_adm, seen_roll, seen_phone = {}, {}, {}
    good, problems, skipped = [], [], []
    new_phones, linked_phones, no_phone = set(), set(), 0

    for record in records:
        check, n = _Checker(record["values"]), record["row"]
        adm = check.text("admission_no", "Admission number", required=True, max_len=30)
        if adm:
            if adm.casefold() in seen_adm:
                check.errors.append(f"Admission number {adm} is repeated (also on row {seen_adm[adm.casefold()]}).")
            else:
                seen_adm[adm.casefold()] = n
        first = check.text("first_name", "First name", required=True)
        last = check.text("last_name", "Last name")
        gender = check.gender()
        dob = check.date("date_of_birth", "Date of birth", sanity=dob_range)
        admitted = check.date("admission_date", "Admission date")
        address = check.text("address", "Address", max_len=300)

        grade = section = None
        class_text = check.values.get("class", "")
        if not class_text:
            check.errors.append("Class is required.")
        else:
            grade = grades.get(class_key(class_text))
            if grade is None:
                check.errors.append(f"Unknown class '{class_text}'. This school's classes: {', '.join(g.name for g in sorted(grades.values(), key=lambda g: g.order))}.")
        section_text = check.values.get("section", "")
        if not section_text:
            check.errors.append("Section is required.")
        elif grade is not None:
            section = sections.get((grade.pk, section_text.casefold()))
            if section is None:
                check.errors.append(f"{grade.name} has no section '{section_text}' this year. Sections: {', '.join(sorted(sections_of.get(grade.pk, []))) or 'none'}.")

        roll = None
        roll_text = check.values.get("roll_no", "")
        if roll_text:
            if not roll_text.isdigit() or not 1 <= int(roll_text) <= 999:
                check.errors.append("Roll number should be a whole number from 1 to 999.")
            elif section is not None:
                roll = int(roll_text)
                if roll in taken_rolls.get(section.pk, set()):
                    check.errors.append(f"Roll number {roll} is already used in {section}.")
                elif (section.pk, roll) in seen_roll:
                    check.errors.append(f"Roll number {roll} is repeated in {section} (also on row {seen_roll[(section.pk, roll)]}).")
                else:
                    seen_roll[(section.pk, roll)] = n

        guardians = []
        for suffix, primary in (("", True), ("2", False)):
            name = check.text(f"guardian{suffix}_name", "Parent/guardian name")
            phone_text = check.values.get(f"guardian{suffix}_phone", "")
            email = check.text(f"guardian{suffix}_email", "Parent email", max_len=254) if not suffix else ""
            relation_text = check.values.get(f"guardian{suffix}_relation", "").lower()
            if not (name or phone_text):
                continue
            phone = check.phone(phone_text, "Parent phone")
            if email:
                try:
                    validate_email(email)
                except ValidationError:
                    check.errors.append(f"Parent email '{email}' doesn't look right.")
                    email = ""
            if phone_text and not name:
                check.errors.append("Parent phone is given but the parent's name is missing.")
            if relation_text not in RELATIONS:
                check.warnings.append(f"Relation '{relation_text}' isn't Father, Mother or Guardian: using Guardian.")
            if name and not phone_text:
                check.warnings.append(f"{name} has no phone number, so no parent login can be made for them.")
            if phone and any(g["phone"] == phone for g in guardians):
                check.warnings.append("The second parent has the same phone number as the first: using one record.")
                continue
            if phone and phone in seen_phone and seen_phone[phone][0].casefold() != name.casefold():
                check.warnings.append(f"Phone {phone} is also used by '{seen_phone[phone][0]}' (row {seen_phone[phone][1]}): treated as the same parent.")
            if phone:
                seen_phone.setdefault(phone, (name, n))
            guardians.append({"name": name, "phone": phone, "email": email, "relation": RELATIONS.get(relation_text, "guardian"), "primary": primary and not guardians})
        if not guardians:
            check.warnings.append("No parent or guardian given.")

        if check.errors:
            problems.append({"row": n, "errors": check.errors, "raw": record["values"]})
            continue
        if adm.casefold() in existing:
            skipped.append({"row": n, "admission_no": adm, "name": f"{first} {last}".strip(), "reason": "Admission number already exists"})
            continue
        for g in guardians:
            if not g["phone"]:
                no_phone += 1
            elif g["phone"] in known_guardians:
                linked_phones.add(g["phone"])
            else:
                new_phones.add(g["phone"])
        good.append(
            {
                "row": n, "admission_no": adm, "first_name": first, "last_name": last, "gender": gender, "date_of_birth": _iso(dob),
                "admission_date": _iso(admitted), "grade_id": grade.pk, "section_id": section.pk, "section_label": str(section),
                "roll_no": roll, "address": address, "guardians": guardians, "warnings": check.warnings,
            }
        )

    summary = {
        "rows": len(records), "new_students": len(good), "problems": len(problems), "skipped_existing": len(skipped),
        "new_parents": len(new_phones), "existing_parents_linked": len(linked_phones), "parents_without_phone": no_phone,
        "warnings": sum(len(g["warnings"]) for g in good),
    }
    return good, problems, skipped, summary


def stage_students(school, user, filename, raw):
    """Read and check an uploaded file and save the result as a batch waiting for confirmation."""
    records, ignored = read_csv(raw, STUDENT_COLUMNS, STUDENT_REQUIRED, STUDENT_SYNONYMS)
    good, problems, skipped, summary = check_students(school, records)
    summary["ignored_columns"] = ignored
    summary["skipped"] = skipped[:200]
    return ImportBatch.objects.create(
        school=school, kind=ImportBatch.Kind.STUDENTS, filename=filename[:200], created_by=user, rows=good, problems=problems, summary=summary
    )


@transaction.atomic
def apply_students(batch):
    """Create the previewed students, parents and enrolments. Returns what was done (also saved on the batch)."""
    batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.status != ImportBatch.Status.PREVIEW:
        raise ImportFileError("This import has already been applied.")
    school = batch.school
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    if year is None:
        raise ImportFileError("This school has no current academic year.")
    sections = Section.objects.filter(school=school, academic_year=year).in_bulk()
    existing = {a.casefold() for a in Student.objects.filter(school=school).values_list("admission_no", flat=True)}
    guardians = {}
    for g in Guardian.objects.filter(school=school).exclude(phone="").order_by("pk"):
        guardians.setdefault(normalize_phone(g.phone), g)
    next_roll = {}
    for section_id, roll in Enrollment.objects.filter(school=school, academic_year=year, roll_no__isnull=False).values_list("section_id", "roll_no"):
        next_roll[section_id] = max(next_roll.get(section_id, 0), roll)
    for row in batch.rows:  # explicit rolls in this file also count, so automatic ones never collide with them
        if row["roll_no"]:
            next_roll[row["section_id"]] = max(next_roll.get(row["section_id"], 0), row["roll_no"])

    created = skipped = parents_created = parents_linked = 0
    student_ids, guardian_ids = [], []
    for row in batch.rows:
        section = sections.get(row["section_id"])
        if section is None or row["admission_no"].casefold() in existing:
            skipped += 1  # the school's data changed between preview and confirmation: stay safe
            continue
        student = Student.objects.create(
            school=school, admission_no=row["admission_no"], first_name=row["first_name"], last_name=row["last_name"],
            gender=row["gender"], date_of_birth=date.fromisoformat(row["date_of_birth"]) if row["date_of_birth"] else None,
            admission_date=date.fromisoformat(row["admission_date"]) if row["admission_date"] else None, address=row["address"],
        )
        existing.add(row["admission_no"].casefold())
        roll = row["roll_no"]
        if not roll:
            next_roll[section.pk] = next_roll.get(section.pk, 0) + 1
            roll = next_roll[section.pk]
        Enrollment.objects.create(school=school, student=student, section=section, roll_no=roll)
        student_ids.append(student.pk)
        created += 1
        for g in row["guardians"]:
            guardian = guardians.get(g["phone"]) if g["phone"] else None
            if guardian is None:
                first, last = split_name(g["name"])
                guardian = Guardian.objects.create(
                    school=school, first_name=first, last_name=last, phone=g["phone"], email=g["email"], address=row["address"],
                    gender={"father": "M", "mother": "F"}.get(g["relation"], ""),
                )
                if g["phone"]:
                    guardians[g["phone"]] = guardian
                parents_created += 1
                guardian_ids.append(guardian.pk)
            else:
                parents_linked += 1
            StudentGuardian.objects.get_or_create(
                school=school, student=student, guardian=guardian, defaults={"relation": g["relation"], "is_primary": g["primary"]}
            )

    batch.status = ImportBatch.Status.APPLIED
    batch.applied_at = timezone.now()
    batch.result = {
        "students": created, "skipped": skipped, "parents_created": parents_created, "parents_linked": parents_linked,
        "student_ids": student_ids, "new_guardian_ids": guardian_ids,
    }
    batch.save(update_fields=["status", "applied_at", "result"])
    return batch.result


# -- staff ----------------------------------------------------------------------------------------------------

def check_staff(school, records):
    existing = {c.casefold() for c in Staff.objects.filter(school=school).values_list("employee_code", flat=True)}
    now = today()
    seen, good, problems, skipped = {}, [], [], []
    for record in records:
        check, n = _Checker(record["values"]), record["row"]
        code = check.text("employee_code", "Employee code", required=True, max_len=30)
        if code:
            if code.casefold() in seen:
                check.errors.append(f"Employee code {code} is repeated (also on row {seen[code.casefold()]}).")
            else:
                seen[code.casefold()] = n
        first = check.text("first_name", "First name", required=True)
        last = check.text("last_name", "Last name")
        gender = check.gender()
        phone = check.phone(check.values.get("phone", ""), "Phone")
        email = check.text("email", "Email", max_len=254)
        if email:
            try:
                validate_email(email)
            except ValidationError:
                check.errors.append(f"Email '{email}' doesn't look right.")
                email = ""
        designation = check.text("designation", "Designation", max_len=80)
        joined = check.date("date_joined", "Date joined", sanity=(date(1960, 1, 1), now))
        if check.errors:
            problems.append({"row": n, "errors": check.errors, "raw": record["values"]})
        elif code.casefold() in existing:
            skipped.append({"row": n, "employee_code": code, "name": f"{first} {last}".strip(), "reason": "Employee code already exists"})
        else:
            good.append({"row": n, "employee_code": code, "first_name": first, "last_name": last, "gender": gender, "phone": phone,
                         "email": email, "designation": designation, "date_joined": _iso(joined), "warnings": check.warnings})
    summary = {"rows": len(records), "new_staff": len(good), "problems": len(problems), "skipped_existing": len(skipped),
               "warnings": sum(len(g["warnings"]) for g in good)}
    return good, problems, skipped, summary


def stage_staff(school, user, filename, raw):
    records, ignored = read_csv(raw, STAFF_COLUMNS, STAFF_REQUIRED, STAFF_SYNONYMS)
    good, problems, skipped, summary = check_staff(school, records)
    summary["ignored_columns"] = ignored
    summary["skipped"] = skipped[:200]
    return ImportBatch.objects.create(
        school=school, kind=ImportBatch.Kind.STAFF, filename=filename[:200], created_by=user, rows=good, problems=problems, summary=summary
    )


@transaction.atomic
def apply_staff(batch):
    batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.status != ImportBatch.Status.PREVIEW:
        raise ImportFileError("This import has already been applied.")
    school = batch.school
    existing = {c.casefold() for c in Staff.objects.filter(school=school).values_list("employee_code", flat=True)}
    created = skipped = 0
    for row in batch.rows:
        if row["employee_code"].casefold() in existing:
            skipped += 1
            continue
        Staff.objects.create(
            school=school, employee_code=row["employee_code"], first_name=row["first_name"], last_name=row["last_name"],
            gender=row["gender"], phone=row["phone"], email=row["email"], designation=row["designation"],
            date_joined=date.fromisoformat(row["date_joined"]) if row["date_joined"] else None,
        )
        existing.add(row["employee_code"].casefold())
        created += 1
    batch.status = ImportBatch.Status.APPLIED
    batch.applied_at = timezone.now()
    batch.result = {"staff": created, "skipped": skipped}
    batch.save(update_fields=["status", "applied_at", "result"])
    return batch.result


# -- downloads --------------------------------------------------------------------------------------------------

def csv_safe(value):
    """Stop spreadsheet apps from running a cell like '=HYPERLINK(...)' as a formula."""
    text = str(value if value is not None else "")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def template_csv(kind):
    out = io.StringIO()
    writer = csv.writer(out)
    if kind == "students":
        writer.writerow(STUDENT_COLUMNS)
        writer.writerow(["A1001", "Asha", "Verma", "Female", "2015-04-23", "2026-04-01", "Class V", "A", "", "12 Lakeview Nagar",
                         "Ravi Verma", "9876543210", "Father", "", "Sunita Verma", "9876543211", "Mother"])
    else:
        writer.writerow(STAFF_COLUMNS)
        writer.writerow(["T101", "Meena", "Rao", "Female", "9876543212", "meena@example.com", "Teacher", "2024-06-15"])
    return out.getvalue()


def problems_csv(batch):
    """Just the rows that failed, with the reasons, ready to fix and upload again."""
    columns = STUDENT_COLUMNS if batch.kind == ImportBatch.Kind.STUDENTS else STAFF_COLUMNS
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["problems", *columns])
    for problem in batch.problems:
        raw = problem["raw"]
        writer.writerow([csv_safe("; ".join(problem["errors"])), *(csv_safe(raw.get(c, "")) for c in columns)])
    return out.getvalue()
