from .base import *
from .guards import check_database, check_production_config, trusted_origins

DEBUG = False

# Running without email is a decision, not an oversight -- see guards.py.
# Nothing is sent: a forgotten password has to be reset by an admin, and an
# invitation link has to be handed over by hand.
EMAIL_OFF = env.bool("EMAIL_OFF", default=False)

check_production_config(SECRET_KEY, ALLOWED_HOSTS, EMAIL_HOST, email_off=EMAIL_OFF)
check_database(DATABASES["default"])

# Hashed filenames and long cache headers. Needs `collectstatic` to have run,
# which is why it is not in base.
STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# nginx sits in front and appends the peer it saw.
TRUSTED_PROXY_HOPS = env.int("TRUSTED_PROXY_HOPS", default=1)

# The domains are already listed in ALLOWED_HOSTS; trust them over https,
# and let the environment override for anything unusual.
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS", default=trusted_origins(ALLOWED_HOSTS)
)

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"

# A shop leaving a till logged in overnight is a real risk; sessions expire.
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
