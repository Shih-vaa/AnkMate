from django.apps import AppConfig


class NoticesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notices"
    verbose_name = "Notices and reminders"

    def ready(self):
        from . import receivers  # noqa: F401  (connects the absence alerts to the attendance signal)
