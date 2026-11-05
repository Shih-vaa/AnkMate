"""Shared test fixture: two small schools so every test can also prove isolation between them.

School A "Alpha"
  Class 5: sections 5A (class teacher t1), 5B (math taught by t2)       Class 6: section 6A (science taught by t3)
  Students: Asha(5A, has login), Bimal(5A), Chitra(5B), Dev(6A)
  Parents:  p1 -> Asha + Chitra (siblings), p2 -> Bimal, 'dual' is ALSO a guardian of Bimal and a teacher of 6A
School B "Beta": one student, Zoya, with her own parent and admin.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings

from academics.models import AcademicYear, Enrollment, Grade, Section, Subject, TeachingAssignment
from accounts.defaults import ensure_default_roles
from accounts.models import Membership, User
from core.models import School
from fees.models import FeeHead, FeeStructure
from people.models import Guardian, Staff, Student, StudentGuardian

PASSWORD = "test-pass-123"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorldTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school_a = School.objects.create(name="Alpha School", code="alpha")
        cls.school_b = School.objects.create(name="Beta School", code="beta")
        cls.roles_a = ensure_default_roles(cls.school_a)
        cls.roles_b = ensure_default_roles(cls.school_b)

        def new_year(school):
            return AcademicYear.objects.create(
                school=school, name="2026-27", start_date=date(2026, 4, 1), end_date=date(2027, 3, 31), is_current=True
            )

        cls.year_a, cls.year_b = new_year(cls.school_a), new_year(cls.school_b)

        cls.grade5 = Grade.objects.create(school=cls.school_a, name="Class 5", order=1, stage="Primary")
        cls.grade6 = Grade.objects.create(school=cls.school_a, name="Class 6", order=2, stage="Middle")
        cls.grade5_b = Grade.objects.create(school=cls.school_b, name="Class 5", order=1, stage="Primary")
        cls.math = Subject.objects.create(school=cls.school_a, code="MATH", name="Mathematics")
        cls.science = Subject.objects.create(school=cls.school_a, code="SCI", name="Science")

        mk = lambda g, n, y=cls.year_a, s=cls.school_a: Section.objects.create(school=s, academic_year=y, grade=g, name=n)
        cls.sec5a, cls.sec5b, cls.sec6a = mk(cls.grade5, "A"), mk(cls.grade5, "B"), mk(cls.grade6, "A")
        cls.sec_b = mk(cls.grade5_b, "A", cls.year_b, cls.school_b)

        # -- people ------------------------------------------------------------------------
        def user(username, **kw):
            return User.objects.create_user(username=username, password=PASSWORD, **kw)

        def grant(u, roles, code):
            Membership.objects.create(user=u, role=roles[code])

        cls.u_admin = user("admin_a", is_staff=True)
        cls.u_office = user("office_a", is_staff=True)
        cls.u_accountant = user("accountant_a", is_staff=True)
        grant(cls.u_admin, cls.roles_a, "admin")
        grant(cls.u_office, cls.roles_a, "office")
        grant(cls.u_accountant, cls.roles_a, "accountant")

        def teacher(username, code, first):
            u = user(username)
            grant(u, cls.roles_a, "teacher")
            return u, Staff.objects.create(school=cls.school_a, user=u, employee_code=code, first_name=first)

        cls.u_t1, cls.t1 = teacher("t1", "T1", "Tara")
        cls.u_t2, cls.t2 = teacher("t2", "T2", "Tej")
        cls.u_t3, cls.t3 = teacher("t3", "T3", "Tina")  # teaches nothing yet
        cls.sec5a.class_teacher = cls.t1
        cls.sec5a.save()
        TeachingAssignment.objects.create(school=cls.school_a, section=cls.sec5b, subject=cls.math, teacher=cls.t2)

        # 'dual' = subject teacher of 6A AND a parent of Bimal
        cls.u_dual = user("dual")
        grant(cls.u_dual, cls.roles_a, "teacher")
        grant(cls.u_dual, cls.roles_a, "parent")
        cls.dual_staff = Staff.objects.create(school=cls.school_a, user=cls.u_dual, employee_code="T4", first_name="Dual")
        TeachingAssignment.objects.create(school=cls.school_a, section=cls.sec6a, subject=cls.science, teacher=cls.dual_staff)

        def student(adm, first, section, roll, school=cls.school_a, u=None):
            s = Student.objects.create(school=school, admission_no=adm, first_name=first, last_name="Test", user=u)
            Enrollment.objects.create(school=school, student=s, section=section, roll_no=roll)
            return s

        cls.u_student = user("asha_login")
        grant(cls.u_student, cls.roles_a, "student")
        cls.asha = student("A1", "Asha", cls.sec5a, 1, u=cls.u_student)
        cls.bimal = student("A2", "Bimal", cls.sec5a, 2)
        cls.chitra = student("A3", "Chitra", cls.sec5b, 1)
        cls.dev = student("A4", "Dev", cls.sec6a, 1)

        cls.u_p1 = user("p1", phone="9876543210")
        cls.u_p2 = user("p2", phone="9876543211")
        grant(cls.u_p1, cls.roles_a, "parent")
        grant(cls.u_p2, cls.roles_a, "parent")

        def guardian(u, first):
            return Guardian.objects.create(school=cls.school_a, user=u, first_name=first, phone=u.phone or "")

        g1, g2, g_dual = guardian(cls.u_p1, "Pia"), guardian(cls.u_p2, "Paul"), guardian(cls.u_dual, "Dual")
        for stu, g in [(cls.asha, g1), (cls.chitra, g1), (cls.bimal, g2), (cls.bimal, g_dual)]:
            StudentGuardian.objects.create(school=cls.school_a, student=stu, guardian=g, relation="guardian")

        # -- school B --------------------------------------------------------------------------
        cls.u_admin_b = user("admin_b", is_staff=True)
        grant(cls.u_admin_b, cls.roles_b, "admin")
        cls.zoya = student("B1", "Zoya", cls.sec_b, 1, school=cls.school_b)
        cls.u_pb = user("pb")
        grant(cls.u_pb, cls.roles_b, "parent")
        gb = Guardian.objects.create(school=cls.school_b, user=cls.u_pb, first_name="Pb")
        StudentGuardian.objects.create(school=cls.school_b, student=cls.zoya, guardian=gb)

        cls.u_orphan = user("orphan")  # has an account but no school role

        # -- fees ---------------------------------------------------------------------------
        head = FeeHead.objects.create(school=cls.school_a, code="TUI", name="Tuition Fee")
        for grade, amount in [(cls.grade5, "1000"), (cls.grade6, "2000")]:
            FeeStructure.objects.create(
                school=cls.school_a, academic_year=cls.year_a, grade=grade, head=head, amount=Decimal(amount), frequency="monthly"
            )

    def admission_nos(self, queryset):
        return set(queryset.values_list("admission_no", flat=True))
