from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    label = "notifications"

    def ready(self):
        # Importing registers the event listeners that queue messages.
        from apps.notifications import services  # noqa: F401
