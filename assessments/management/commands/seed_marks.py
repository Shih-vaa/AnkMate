"""Add fake exam results to the demo school.

    python manage.py seed_marks            # first time (also creates the exam structure if missing)
    python manage.py seed_marks --reset    # throw away the marks and regenerate
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from assessments.demo import seed_marks
from core.models import School


class Command(BaseCommand):
    help = "Generate fake exam results for the demo school (never for real data)."

    def add_arguments(self, parser):
        parser.add_argument("--school", default="maple-grove-demo", help="School code (default: the demo school).")
        parser.add_argument("--reset", action="store_true", help="Delete the school's marks first.")
        parser.add_argument("--force", action="store_true", help="Allow running when DEBUG is off.")

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError("Refusing to seed demo data while DEBUG is off (use --force if you really mean it).")
        try:
            school = School.objects.get(code=options["school"])
        except School.DoesNotExist:
            raise CommandError(f"No school with code '{options['school']}'. Run seed_demo first.")
        try:
            counts = seed_marks(school, reset=options["reset"])
        except ValueError as error:
            raise CommandError(str(error))
        self.stdout.write(self.style.SUCCESS(f"Created {counts['marks']} marks and {counts['remarks']} remarks for {school.name}."))
