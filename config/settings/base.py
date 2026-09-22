"""
Base settings shared by every environment.

Anything that differs between machines comes from the environment, never from
a branch in this file.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-key-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "django_htmx",
]

# Order matters only for template/static resolution, not for imports.
LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.tenancy",
    "apps.org",
    "apps.catalog",
    "apps.inventory",
    "apps.pos",
    "apps.purchasing",
    "apps.customers",
    "apps.finance",
    "apps.reports",
    "apps.notifications",
    "apps.sync",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# --------------------------------------------------------------------------
# Middleware
#
# TenantMiddleware must run after AuthenticationMiddleware (it reads
# request.user) and before anything that touches tenant-scoped data.
# SubscriptionMiddleware runs after it, because it reads the resolved tenant.
# --------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "apps.core.middleware.TenantMiddleware",
    "apps.core.middleware.SubscriptionMiddleware",
    "apps.core.middleware.PlanLimitMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.tenant",
                "apps.core.context_processors.navigation",
                "apps.core.context_processors.platform_nav",
            ],
            "builtins": [
                "apps.core.templatetags.perms",
                "apps.core.templatetags.icons",
                # Money is formatted in partials that extend nothing, so a
                # {% load %} in the page template never reaches them.
                "django.contrib.humanize.templatetags.humanize",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# Database
#
# One shared Postgres. Row-Level Security is applied by migration; the
# application connects as a non-superuser so those policies are enforced.
# --------------------------------------------------------------------------

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://retail_app:retail_app@127.0.0.1:5432/retail",
    )
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Whether the sign-in page offers "start a free trial". Signing up still
# works and the address is still open; this only decides whether the door is
# advertised there, which is a decision about selling, not about security.
SHOW_SIGNUP_LINK = env.bool("SHOW_SIGNUP_LINK", default=False)

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# --------------------------------------------------------------------------
# Cache / queue
# --------------------------------------------------------------------------

REDIS_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TIMEZONE = "Africa/Dar_es_Salaam"

# --------------------------------------------------------------------------
# I18N / time
# --------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Dar_es_Salaam"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# Static / media
# --------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# --------------------------------------------------------------------------
# Email
#
# Password resets and invitations are sent from here. Without a host the
# backend falls back to writing them to the log, so a reset silently did
# nothing in production rather than failing loudly.
# --------------------------------------------------------------------------
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@localhost")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend"
)

# How many proxies of ours sit in front of the app. Each one appends the
# address it saw to X-Forwarded-For, so the client's real address is that
# many entries from the right. With none, the header is not trusted at all:
# anyone can put anything in it.
TRUSTED_PROXY_HOPS = env.int("TRUSTED_PROXY_HOPS", default=0)

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
# Receipts and other paperwork. Never under MEDIA_ROOT, never served by nginx.
PRIVATE_MEDIA_ROOT = env.path("PRIVATE_MEDIA_ROOT", default=BASE_DIR / "private")

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# --------------------------------------------------------------------------
# Platform behaviour
# --------------------------------------------------------------------------

# Days a tenant keeps working after the subscription lapses before the system
# drops them to read-only. Data is never deleted on non-payment.
SUBSCRIPTION_GRACE_DAYS = env.int("SUBSCRIPTION_GRACE_DAYS", default=7)

# How long a resolved permission set is cached per membership.
PERMISSION_CACHE_SECONDS = env.int("PERMISSION_CACHE_SECONDS", default=300)
