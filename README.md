# AnkMate

School management for one school today, many schools tomorrow: students, parents, staff, classes,
fee structure, with role-based access. One installable web app (PWA) that works as a desktop
dashboard for teachers/office and as a phone app for parents and students.

**Status: foundation + attendance + fees + marks + demo.** Done: multi-school core, users/roles/scoped
permissions, academic structure, people records, ICSE preset, daily attendance (marking, parent and
student views, class registers), fee structure, monthly invoices, concessions, payments with receipts,
reversals, dues list, exams and marks entry, class marksheets with analysis, student progress pages,
printable report cards, responsive PWA shell, tests.
Not yet: online payment (gateway), notices, absence and fee reminders to parents, late fines, bulk import,
class ranks, an audit log of mark changes.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo      # fake Maple Grove School (ICSE) data + attendance + fee + marks history
.venv/bin/python manage.py runserver
.venv/bin/python manage.py test           # 369 tests
```

If you already have a demo database from before attendance existed, run `manage.py migrate` and then
`manage.py seed_attendance` (add `--reset` to regenerate). The same goes for fees (`manage.py seed_fees`) and marks (`manage.py seed_marks`); each takes `--reset` to regenerate.

Open http://localhost:8000/ . On a phone on the same Wi-Fi, run
`runserver 0.0.0.0:8000` and add that host to `DJANGO_ALLOWED_HOSTS`.

## Demo logins (fake data, dev only)

Password for **every** demo account: `demo@1234`

| Role | Login |
|---|---|
| Principal / admin | `principal` |
| Accountant | `accounts` |
| Office staff | `office` |
| Teacher (class teacher + subjects) | `teacher01` ... `teacher26` |
| Parent | phone number, e.g. `0000000001` (fake numbers start with 0000) |
| Student (Class VI and above) | admission number, e.g. `mgs260129` |

`principal`, `accounts` and `office` can also open **Manage data** (Django admin, locked to their school).
Create a platform owner with `manage.py createsuperuser` to manage schools and user accounts.

## How it fits together

| App | Owns |
|---|---|
| `core` | `School`, the `TenantModel` base class (every table has a `school`), presets, demo seed |
| `accounts` | `User` (phone/email/username login), `Role`, `Membership`, the access engine |
| `people` | `Student`, `Guardian`, `Staff`, `StudentGuardian` |
| `academics` | `AcademicYear`, `Grade`, `Section`, `Subject`, `Enrollment` (per year), `TeachingAssignment` |
| `attendance` | `AttendanceRecord` (one per student per day), `Holiday`, the rules in `services.py`, marking/calendar/register screens |
| `assessments` | `Exam`, `ExamPaper` (max marks per exam/class/subject), `Mark`, `GradeBand`, `ReportRemark`, the rules in `services.py`, entry/marksheet/progress/report-card screens |
| `fees` | `FeeHead`, `FeeStructure`, `Concession`, `FeeInvoice` (+ lines), `Payment` (+ allocations), number sequences, the billing and payment rules in `services.py` |
| `web` | pages, templates, stylesheet, PWA manifest + service worker |

### Access control in one paragraph

A **permission** (`fees.collect`, `attendance.mark`, ...) says what you may do; a **scope** says to
whose records: whole school, only the sections you teach, only your own children, or only yourself.
A `Role` is a per-school row holding a scope and a list of permissions, so schools can differ without
code changes. A person can hold several roles (a teacher who is also a parent gets both). Views never
filter by hand: they call `accounts.access.visible_students(user, school, perm)` or `has_perm(...)`,
and a record outside your scope simply returns 404.

### Attendance rules

Daily attendance, one record per student per day: **P**resent, **A**bsent, **L**ate, **H**alf day, **E** (on leave).

| Rule | Default | Where to change it |
|---|---|---|
| Who takes attendance | the class teacher of that section; the principal can take any class | `School.settings["attendance_marked_by"]` = `"class_teacher"` or `"assigned_teachers"` (any teacher assigned to the section) |
| How far back a teacher may edit | 7 days (principal/office: any date in the year) | `School.settings["attendance_edit_window_days"]` |
| Closed days | Sundays, plus the `Holiday` list | `School.settings["weekly_off"]` (Monday=0 ... Sunday=6), `Holiday` rows in the admin |
| Future dates, or outside the academic year | never allowed | |
| Percentage | Present and Late = 1 day, Half day = 0.5, Absent and On leave = 0, over days attendance was taken | `attendance/services.py` (`WEIGHT`) |

Who sees what: parents and students see only their own children's/own calendar and percentages; teachers
see registers for the classes they teach but can only *take* attendance for the classes the rule above allows;
the principal and office see everything in their school. Edit the `Role` rows (or permissions
`attendance.view` / `attendance.mark`) to change this per school.

### Fees: how the money works

**Billing.** Invoices are monthly. The accountant (or principal) opens *Fees > Generate invoices*, picks a
month, sees a preview, and creates them. It bills every active, enrolled student from the fee structure and
applies that student's concessions. Running it again never double-bills: students who already have a live
invoice for the month are skipped. A fee head's frequency decides which months it appears in, counting from
the start of the session (April = month 0): monthly = every month, quarterly = months 0/3/6/9, half-yearly =
months 0 and 6, annual = month 0, one-time = the first invoice from the student's admission month (only for
students admitted this session). Invoices fall due on the `School.settings["fee_due_day"]` (default 10th).

**Concessions** (scholarships, sibling discounts) are rows in the admin: a percentage of the matching charges,
or a fixed amount off each invoice, for one fee head or all. They apply to invoices generated *after* they
are added; issued invoices never change. A student is never billed less than zero.

**Payments.** Office staff record what they receive (cash, UPI, cheque, bank transfer, card) on *Fees > Collect
payment*. The money is applied to the oldest dues first. You cannot record more than is owed, a double-click
cannot record it twice (one-time form token), and every receipt gets the next number in a gap-free series
(`RCPT/2026-27/000123`), printable, with the amount in words.

**The ledger is append-only.** Amounts on an issued invoice and on a payment can't be edited, even by the
principal, even in the admin site. Mistakes are fixed with a recorded reason:
*cancel an invoice* (only before any money has been paid against it; generate it again afterwards) or
*reverse a payment* (a bounced cheque, the wrong student). The reversed receipt stays on record, stamped
REVERSED, with who, when and why. Balances are never stored: they are always worked out from invoices and
completed payments, so they cannot drift out of step.

| Who | Can |
|---|---|
| Parent | see their own children's invoices, receipts and dues (never anyone else's) |
| Office staff | view everything, collect payments |
| Accountant, principal | all of the above, plus generate invoices, cancel invoices, reverse payments, manage concessions |
| Teacher, student | no access to fees |

Amounts are exact decimals everywhere (never floats), shown with Indian digit grouping (₹12,34,567.50).
Use PostgreSQL for real data: it locks rows properly when two people take payments at once.

### Marks and report cards: how it works

**Exam structure is data.** A school defines its exams for the year (`Exam`: name, order, and its *weightage*: how
much it counts towards the final result, in percent), the maximum marks of each paper (`ExamPaper`: one per exam,
class and subject) and its grade scale (`GradeBand`). The ICSE preset loads a *placeholder* structure (Unit Test 1,
Half-Yearly, Unit Test 2, Annual weighted 10/30/10/50) and a placeholder grade scale (A1 from 91% ... E below 33%);
**verify all of it with the school**. They are edited in *Manage data*. The pass mark is
`School.settings["pass_percent"]` (default 33).

**Entering marks.** A teacher enters marks only for a class and subject they are assigned to teach (their
`TeachingAssignment`); being class teacher alone is not enough. The principal can enter or correct any paper.
A blank box means "not entered yet", `AB` means absent, and the two are never confused with zero. Invalid entries
(over the maximum, negative, not a number) save nothing and point at the student. Every mark records who entered it
and when.

**Draft, published, locked.** Marks are invisible to parents and students until the principal *publishes* the exam
(*Marks > Manage exams*). After publishing, teachers can no longer change marks; the principal still can, until the
exam is *locked*, after which nobody can. Hiding a published exam again is possible until it is locked. A teacher who
is also a parent never sees unpublished marks of their own child this way: drafts are only visible for students in
the staff member's own classes.

**How a result is worked out.** A subject's result is the weighted percentage over the exams that have a result for
that student: `sum(marks / max * weightage) / sum(weightage) * 100`. Exams not held or not entered yet are left out
(so mid-year figures are "so far"); an absence counts as zero for that exam. Percentages are rounded to one decimal
first, and grade and pass/fail use that rounded figure, so the printed number always agrees with the decision. The
overall percentage is the plain average of the subject percentages; subjects marked *optional* are shown but left out.

| Who | Can |
|---|---|
| Parent, student | see published results, progress and report card of their own children / themselves |
| Teacher | enter marks for their own class and subject until published; see class marksheets, analysis and drafts for classes they teach; write the class remark if they are the class teacher |
| Principal | everything above for any class, plus publish, hide, lock, and define exams in the admin |

The report card (printable) shows the school, student, marks per exam with weightage, final percentage and grade,
the result, attendance for the session, the class teacher's remark and the grade scale. Staff can preview it with
unpublished marks; parents never can.

### Adding another school or board

1. Write a preset like `core/presets/icse.py` (grades, subjects, fee heads) and register it in `core/presets/loader.py`.
2. Create the `School` row (admin site, superuser) and call `apply_preset(school, "<key>")`.
3. Everything after that is editable data. No code changes.

## Before real use (not done yet)

- Switch to PostgreSQL, set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS`, serve static files (WhiteNoise or nginx), HTTPS.
- Account provisioning for parents/teachers by office staff (today only the platform owner creates accounts).
- Serve static files with hashed filenames (WhiteNoise `CompressedManifestStaticFilesStorage`), so a changed stylesheet is never served stale by the service worker.
- Online payment: needs a payment-gateway account in the *school's* name (money must settle into the school's bank account, never yours). `Payment.method = "online"` and the idempotency key are already in place for it.
- Fee reminders and absence alerts to parents (SMS/WhatsApp), and a decision on who pays for those messages.
- Marks: confirm the exam structure, weightages, pass mark and grade scale with the school (and whether non-scholastic subjects like Art or PE are graded rather than marked); class ranks and an audit trail of every mark change are not built yet.
- Late fines, receipt PDFs/GST needs if the school requires them, and a financial year-end/closing process (confirm with the school's accountant).
- Password reset flow (SMS/WhatsApp cost decisions), rate limiting on login.
- Backups, an audit log of who changed what, and a written data agreement with the school.
- Verify the ICSE class/subject lists and replace dummy fee amounts with the school's real ones.
