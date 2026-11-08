from django.db import transaction
from django.dispatch import receiver

from attendance.signals import attendance_saved

from . import reminders


@receiver(attendance_saved, dispatch_uid="notices-absence-alerts")
def send_absence_alerts(sender, section, on_date, marks, user, **kwargs):
    # The savepoint means a failure here undoes only the alerts, never the attendance that was just saved.
    with transaction.atomic():
        reminders.absence_alerts(section, on_date, marks, user)
