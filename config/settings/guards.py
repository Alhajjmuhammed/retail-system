"""
Configuration the application refuses to start without.

Separate from prod.py so these can be tested without importing production
settings, which would run the checks against whatever happens to be in the
environment at the time.
"""

from django.core.exceptions import ImproperlyConfigured

DEV_KEYS = {"", "change-me", "dev-only-insecure-key-change-me"}
DEV_HOSTS = {"", "localhost", "127.0.0.1", "0.0.0.0", "*"}
MIN_KEY_LENGTH = 50


def check_production_config(secret_key, allowed_hosts, email_host=None):
    """
    Refuse to start rather than run on development defaults.

    A shared SECRET_KEY means forged sessions, and silently falling back to
    one is the kind of thing nobody notices until it matters. A wildcard host
    means the site answers to any domain pointed at it.
    """
    if secret_key in DEV_KEYS or len(secret_key) < MIN_KEY_LENGTH:
        raise ImproperlyConfigured(
            "SECRET_KEY must be set to a long random value in production. "
            'Generate one with: python -c "from '
            'django.core.management.utils import get_random_secret_key as k; '
            'print(k())"'
        )

    hosts = [host.strip() for host in (allowed_hosts or [])]
    if not hosts or all(host in DEV_HOSTS for host in hosts):
        raise ImproperlyConfigured(
            "ALLOWED_HOSTS must list the real domains this serves."
        )

    if email_host is not None and not email_host:
        # Password resets and invitations go by email: with no host they
        # were written to the log and nobody ever received them.
        raise ImproperlyConfigured(
            "EMAIL_HOST must be set in production, or nobody can reset a password."
        )


def trusted_origins(allowed_hosts):
    """
    The origins a POST may come from, taken from the domains this serves.

    Django checks the Origin of every POST against the host it believes it
    is serving. Behind a proxy, on a non-standard port, or across subdomains
    that check fails, and it fails for every form in the system at once --
    signing in, selling, syncing a till -- with nothing in the log to say
    why. A leading dot means "and its subdomains", which is a host pattern,
    not an origin, so it is dropped.
    """
    origins = []
    for host in allowed_hosts or []:
        host = host.strip().lstrip(".")
        if not host or host in DEV_HOSTS:
            continue
        origins.append(f"https://{host}")
    return origins
