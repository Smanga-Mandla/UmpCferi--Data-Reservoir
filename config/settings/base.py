import os
import secrets
from pathlib import Path

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
import base64
import hashlib

from config.settings.database import database_config_from_env

BASE_DIR = Path(__file__).resolve().parent.parent.parent

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set outside development.")
    SECRET_KEY = secrets.token_urlsafe(50)
MFA_ENCRYPTION_KEY = os.environ.get("MFA_ENCRYPTION_KEY") or base64.urlsafe_b64encode(
    hashlib.sha256(f"{SECRET_KEY}:ump-cferi-mfa".encode()).digest()
).decode()

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts.apps.AccountsConfig",
    "documents.apps.DocumentsConfig",
    "partners.apps.PartnersConfig",
    "stakeholders.apps.StakeholdersConfig",
    "incubation.apps.IncubationConfig",
    "interactions.apps.InteractionsConfig",
    "reporting.apps.ReportingConfig",
    "audit.apps.AuditConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "audit.middleware.AuditActorMiddleware",
    "accounts.middleware.AccessControlMiddleware",
    "accounts.middleware.InactivityTimeoutMiddleware",
    "accounts.middleware.MfaEnforcementMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = database_config_from_env()

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "accounts.backends.RolePermissionBackend",
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]
PASSWORD_RESET_TIMEOUT = 30 * 60
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "reporting:dashboard"
LOGOUT_REDIRECT_URL = "home"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-za"
TIME_ZONE = "Africa/Johannesburg"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
MEDIA_URL = ""
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "var" / "static"
PRIVATE_MEDIA_ROOT = BASE_DIR / "var" / "private-media"
STORAGES = {
    "default": {"BACKEND": "config.storage.PrivateMediaStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "false").lower() == "true"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "")

CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
INCUBATEE_CHECKIN_SEND_DAY = int(os.environ.get("INCUBATEE_CHECKIN_SEND_DAY", "1"))
INCUBATEE_CHECKIN_DEADLINE_DAY = int(os.environ.get("INCUBATEE_CHECKIN_DEADLINE_DAY", "15"))
if not 1 <= INCUBATEE_CHECKIN_SEND_DAY <= 28:
    raise ValueError("INCUBATEE_CHECKIN_SEND_DAY must be between 1 and 28.")
if not 1 <= INCUBATEE_CHECKIN_DEADLINE_DAY <= 28:
    raise ValueError("INCUBATEE_CHECKIN_DEADLINE_DAY must be between 1 and 28.")
INCUBATEE_CHECKIN_REMINDER_DAYS = [
    int(day.strip())
    for day in os.environ.get("INCUBATEE_CHECKIN_REMINDER_DAYS", "").split(",")
    if day.strip()
]
if any(day < 0 or day > 28 for day in INCUBATEE_CHECKIN_REMINDER_DAYS):
    raise ValueError("INCUBATEE_CHECKIN_REMINDER_DAYS entries must be between 0 and 28.")
INCUBATEE_CHECKIN_STATUSES = [
    status.strip()
    for status in os.environ.get("INCUBATEE_CHECKIN_STATUSES", "active").split(",")
    if status.strip()
]
if any(
    status not in {"applicant", "onboarding", "active", "graduated", "exited", "withdrawn"}
    for status in INCUBATEE_CHECKIN_STATUSES
):
    raise ValueError("INCUBATEE_CHECKIN_STATUSES contains an unknown incubatee status.")
INCUBATEE_CHECKIN_LINK_MAX_AGE_SECONDS = int(
    os.environ.get("INCUBATEE_CHECKIN_LINK_MAX_AGE_SECONDS", str(30 * 24 * 60 * 60))
)
if INCUBATEE_CHECKIN_LINK_MAX_AGE_SECONDS <= 0:
    raise ValueError("INCUBATEE_CHECKIN_LINK_MAX_AGE_SECONDS must be positive.")
PUBLIC_BASE_URL = os.environ.get("DJANGO_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
CELERY_BEAT_SCHEDULE = {
    "deactivate-expired-accounts-every-five-minutes": {
        "task": "accounts.tasks.deactivate_expired_accounts",
        "schedule": 300.0,
    },
    "send-centre-document-expiry-alerts-daily": {
        "task": "documents.tasks.send_expiry_alerts",
        "schedule": crontab(hour=6, minute=0),
    },
    "send-mou-renewal-alerts-daily": {
        "task": "partners.tasks.send_mou_expiry_alerts",
        "schedule": crontab(hour=6, minute=15),
    },
    "send-incubatee-checkin-links-daily": {
        "task": "incubation.tasks.send_monthly_checkin_links",
        "schedule": crontab(hour=6, minute=30),
    },
    "send-incubatee-checkin-reminders-daily": {
        "task": "incubation.tasks.send_checkin_reminders",
        "schedule": crontab(hour=6, minute=35),
    },
    "create-due-incubatee-followups-daily": {
        "task": "incubation.tasks.create_due_incubatee_followups",
        "schedule": crontab(hour=6, minute=40),
    },
    "send-monthly-role-scoped-summary-reports": {
        "task": "reporting.tasks.send_monthly_summary_reports",
        "schedule": crontab(day_of_month=1, hour=7, minute=0),
    },
}
DOCUMENT_EXPIRY_ALERT_RECIPIENTS = [
    address.strip()
    for address in os.environ.get("DOCUMENT_EXPIRY_ALERT_RECIPIENTS", "").split(",")
    if address.strip()
]
MOU_ALERT_RECIPIENTS = [
    address.strip()
    for address in os.environ.get("MOU_ALERT_RECIPIENTS", "").split(",")
    if address.strip()
]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "config.logging.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("LOG_LEVEL", "INFO"),
    },
}

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
