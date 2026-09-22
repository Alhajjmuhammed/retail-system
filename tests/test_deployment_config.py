"""
What production refuses to start without, and what it must not forget.

These are the settings that fail on the first day rather than in a test: a
shared secret key, a wildcard host, and the CSRF origin check that turns
every form in the system into "CSRF verification failed" with nothing in the
log to say why.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings.guards import check_production_config

GOOD_KEY = "x" * 60


def test_a_development_secret_key_stops_the_boot():
    for bad in ("", "change-me", "dev-only-insecure-key-change-me", "short"):
        with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
            check_production_config(bad, ["shop.example.com"], "smtp.example.com")


def test_a_wildcard_host_stops_the_boot():
    for hosts in ([], ["*"], ["localhost"], ["127.0.0.1", "0.0.0.0"]):
        with pytest.raises(ImproperlyConfigured, match="ALLOWED_HOSTS"):
            check_production_config(GOOD_KEY, hosts, "smtp.example.com")


def test_no_mail_host_stops_the_boot():
    """Password resets and invitations go by email; without one they vanish."""
    with pytest.raises(ImproperlyConfigured, match="EMAIL_HOST"):
        check_production_config(GOOD_KEY, ["shop.example.com"], "")


def test_a_real_configuration_is_accepted():
    check_production_config(GOOD_KEY, ["shop.example.com"], "smtp.example.com")


def test_the_forms_will_work_behind_a_proxy():
    """
    Django checks a POST's Origin against the host it believes it serves.
    Behind nginx and HTTPS that check fails unless the domains are trusted,
    and it fails for every form at once.
    """
    from config.settings.guards import trusted_origins

    assert trusted_origins(["shop.example.com", "www.shop.example.com"]) == [
        "https://shop.example.com", "https://www.shop.example.com",
    ]
    # A wildcard subdomain host is a host pattern, not an origin.
    assert trusted_origins([".shop.example.com"]) == ["https://shop.example.com"]
    # And nothing local is trusted in production.
    assert trusted_origins(["localhost", "127.0.0.1", "*"]) == []


def test_production_locks_the_doors():
    """Read from the file rather than imported: importing it needs the real
    environment, and these are the lines that must not quietly change."""
    from pathlib import Path

    source = Path("config/settings/prod.py").read_text()
    for setting in ("DEBUG = False", "SESSION_COOKIE_SECURE = True",
                    "CSRF_COOKIE_SECURE = True", "SECURE_SSL_REDIRECT = True",
                    "SECURE_HSTS_SECONDS = 31536000", 'X_FRAME_OPTIONS = "DENY"',
                    "CSRF_TRUSTED_ORIGINS"):
        assert setting in source, setting
