"""Add fake invoices, concessions and payments to the demo school.

    python manage.py seed_fees            # first time
    python manage.py seed_fees --reset    # throw away the fee ledger and regenerate
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import School
from fees.demo import seed_fees


class Command(BaseCommand):
    help = "Generate fake fee invoices and payments for the demo school (never for real data)."

    def add_arguments(self, parser):
        parser.add_argument("--school", default="maple-grove-demo", help="School code (default: the demo school).")
        parser.add_argument("--reset", action="store_true", help="Delete the school's fee ledger first.")
        parser.add_argument("--force", action="store_true", help="Allow running when DEBUG is off.")

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError("Refusing to seed demo data while DEBUG is off (use --force if you really mean it).")
        try:
            school = School.objects.get(code=options["school"])
        except School.DoesNotExist:
            raise CommandError(f"No school with code '{options['school']}'. Run seed_demo first.")
        try:
            counts = seed_fees(school, reset=options["reset"])
        except ValueError as error:
            raise CommandError(str(error))
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {counts['invoices']} invoices, {counts['payments']} payments and "
                f"{counts['concessions']} concessions for {school.name}."
            )
        )
