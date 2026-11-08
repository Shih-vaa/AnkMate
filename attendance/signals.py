"""Announcements other parts of the system can listen to, so attendance never has to know who is listening."""
from django.dispatch import Signal

# Sent after a class's attendance for one day has been saved.
#   section: the Section   on_date: the day   marks: {student_id: status code}   user: who saved it
attendance_saved = Signal()
