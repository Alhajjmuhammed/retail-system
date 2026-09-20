from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"

    def ready(self):
        # Declaring the permission catalogue at import time is what lets the
        # role builder, the sync migration and every check() share one source.
        from apps.accounts import permissions  # noqa: F401
