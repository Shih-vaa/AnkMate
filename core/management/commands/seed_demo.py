"""Fill a fresh database with a fictional demo school: Maple Grove School (ICSE).

    python manage.py seed_demo

Everything here is FAKE: invented names, invented phone numbers (they start with 0000, so they
can never be a real mobile number), dummy fees. Never run it against real data.
"""
import random
from datetime import date

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from academics.models import AcademicYear, Enrollment, Section, TeachingAssignment, GradeSubject
from accounts.defaults import ensure_default_roles
from accounts.models import Membership, User
from core.models import School
from core.presets.loader import apply_preset
from people.models import Guardian, Staff, Student, StudentGuardian

DEMO_PASSWORD = "demo@1234"
STUDENTS_PER_SECTION = 8
STUDENT_LOGIN_FROM_GRADE_ORDER = 9  # Class VI and above get their own login

FIRST_M = ["Aarav", "Vivaan", "Aditya", "Arjun", "Reyansh", "Ishaan", "Kabir", "Rohan", "Daniel", "Joseph", "Samuel", "Rahul", "Amit", "Vikas", "Anuj", "Saurabh", "Mohit", "Nikhil", "Imran", "Faisal"]
FIRST_F = ["Ananya", "Diya", "Saanvi", "Aadhya", "Myra", "Kiara", "Ira", "Meera", "Priya", "Neha", "Pooja", "Ritu", "Sneha", "Anjali", "Maria", "Grace", "Rebecca", "Sana", "Fatima", "Kavya"]
SURNAMES = ["Sharma", "Verma", "Gupta", "Mishra", "Tripathi", "Dwivedi", "Yadav", "Singh", "Agarwal", "Pandey", "Srivastava", "Khan", "Ahmed", "Thomas", "Joseph", "Fernandes", "D'Souza", "Dixit", "Shukla", "Chaturvedi"]


class Command(BaseCommand):
    help = "Create the fictional demo school (Maple Grove School) with dummy ICSE data."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Allow running when DEBUG is off.")

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError("Refusing to seed demo data while DEBUG is off (use --force if you really mean it).")
        if School.objects.filter(code="maple-grove-demo").exists():
            raise CommandError("Demo school already exists. Delete db.sqlite3 and run migrate again to start over.")

        rng = random.Random(2026)
        hashed = make_password(DEMO_PASSWORD)  # hash once; hashing ~500 passwords would take minutes
        counters = {"phone": 0, "adm": 0}

        def make_user(username, first, last, phone=None, is_staff=False):
            return User.objects.create(
                username=username, first_name=first, last_name=last, phone=phone, is_staff=is_staff, password=hashed
            )

        with transaction.atomic():
            school = School.objects.create(
                name="Maple Grove School", code="maple-grove-demo", board="ICSE",
                address="Lakeview Nagar", city="Demo City",
            )
            year = apply_preset(school, "icse")
            roles = ensure_default_roles(school)

            def grant(user, role_code):
                Membership.objects.create(user=user, role=roles[role_code])

            # -- office staff --------------------------------------------------------------
            for username, first, last, designation, code, role, is_staff in [
                ("principal", "Demo", "Principal", "Principal", "E001", "admin", True),
                ("accounts", "Demo", "Accountant", "Accountant", "E002", "accountant", True),
                ("office", "Demo", "Office", "Office clerk", "E003", "office", True),
            ]:
                user = make_user(username, first, last, is_staff=is_staff)
                Staff.objects.create(school=school, user=user, employee_code=code, first_name=first, last_name=last, designation=designation)
                grant(user, role)

            # -- teachers: one class teacher per section, plus subject teaching -------------
            sections = list(Section.objects.filter(school=school, academic_year=year).select_related("grade").order_by("grade__order", "name"))
            teachers = []
            for i in range(len(sections)):
                first = rng.choice(FIRST_F + FIRST_M)
                last = rng.choice(SURNAMES)
                user = make_user(f"teacher{i + 1:02d}", first, last)
                staff = Staff.objects.create(
                    school=school, user=user, employee_code=f"T{i + 1:03d}", first_name=first, last_name=last,
                    designation="Teacher", date_joined=date(rng.randint(2008, 2025), 4, 1),
                )
                grant(user, "teacher")
                teachers.append(staff)

            for i, section in enumerate(sections):
                section.class_teacher = teachers[i]
                section.save()
                subject_ids = list(
                    GradeSubject.objects.filter(grade=section.grade).order_by("subject__name").values_list("subject_id", flat=True)
                )
                for j, subject_id in enumerate(subject_ids):
                    TeachingAssignment.objects.create(
                        school=school, section=section, subject_id=subject_id, teacher=teachers[(i + j) % len(teachers)]
                    )

            # -- students, parents, enrolments ---------------------------------------------
            last_guardian = None
            for section in sections:
                for roll in range(1, STUDENTS_PER_SECTION + 1):
                    is_girl = rng.random() < 0.5
                    first = rng.choice(FIRST_F if is_girl else FIRST_M)

                    # ~12% of children share a guardian with the previously created child (siblings)
                    if last_guardian is not None and rng.random() < 0.12:
                        guardian = last_guardian
                    else:
                        counters["phone"] += 1
                        phone = f"0000{counters['phone']:06d}"
                        g_female = rng.random() < 0.5
                        g_first = rng.choice(FIRST_F if g_female else FIRST_M)
                        g_last = rng.choice(SURNAMES)
                        guser = make_user(phone, g_first, g_last, phone=phone)
                        guardian = Guardian.objects.create(
                            school=school, user=guser, first_name=g_first, last_name=g_last,
                            phone=phone, gender="F" if g_female else "M", occupation=rng.choice(["Business", "Teacher", "Engineer", "Doctor", "Government service", "Homemaker"]),
                            address="Lakeview Nagar, Demo City",
                        )
                        grant(guser, "parent")
                        last_guardian = guardian

                    counters["adm"] += 1
                    adm_no = f"MGS{year.start_date.year % 100:02d}{counters['adm']:04d}"
                    age = section.grade.order + 2  # Nursery ~3 yrs ... Class X ~15 yrs
                    dob = date(year.start_date.year - age, rng.randint(1, 12), rng.randint(1, 28))
                    student = Student.objects.create(
                        school=school, admission_no=adm_no, first_name=first, last_name=guardian.last_name,
                        gender="F" if is_girl else "M", date_of_birth=dob, admission_date=date(year.start_date.year - rng.randint(0, 5), 4, 1),
                        address=guardian.address,
                    )
                    if section.grade.order >= STUDENT_LOGIN_FROM_GRADE_ORDER:
                        suser = make_user(adm_no.lower(), first, guardian.last_name)
                        student.user = suser
                        student.save()
                        grant(suser, "student")

                    StudentGuardian.objects.create(
                        school=school, student=student, guardian=guardian,
                        relation="mother" if guardian.gender == "F" else "father", is_primary=True,
                    )
                    Enrollment.objects.create(school=school, student=student, section=section, roll_no=roll)

        self.stdout.write(self.style.SUCCESS(f"Seeded {school.name} ({school.board}), session {year.name}."))
        self.stdout.write(
            f"  {Staff.objects.filter(school=school).count()} staff, {Student.objects.filter(school=school).count()} students, "
            f"{Guardian.objects.filter(school=school).count()} guardians, {Section.objects.filter(school=school).count()} sections."
        )
        self.stdout.write("\nDemo logins (all use the password shown in README.md):")
        self.stdout.write("  Principal/admin : principal")
        self.stdout.write("  Accountant      : accounts")
        self.stdout.write("  Office staff    : office")
        self.stdout.write("  A teacher       : teacher01")
        first_parent = Guardian.objects.filter(school=school).order_by("pk").first()
        self.stdout.write(f"  A parent        : {first_parent.phone}  (phone number login)")
        first_student = Student.objects.filter(school=school, user__isnull=False).order_by("pk").first()
        self.stdout.write(f"  A student       : {first_student.user.username}")
