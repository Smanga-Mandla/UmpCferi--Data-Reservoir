import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from config.settings.validation import validate_public_base_url

os.environ["DJANGO_DEBUG"] = "false"

from config.settings.base import *  # noqa: F403,E402

DEBUG = False
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production.")
if not os.environ.get("DJANGO_ALLOWED_HOSTS"):
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must contain production hostnames.")
if not EMAIL_HOST:
    raise ImproperlyConfigured("EMAIL_HOST must be configured for production.")
if not os.environ.get("PRIVATE_MEDIA_ROOT"):
    raise ImproperlyConfigured(
        "PRIVATE_MEDIA_ROOT must point to persistent private storage in production."
    )
private_media_root = Path(PRIVATE_MEDIA_ROOT).resolve()
static_root = Path(STATIC_ROOT).resolve()
if (
    private_media_root == static_root
    or static_root in private_media_root.parents
    or private_media_root in static_root.parents
):
    raise ImproperlyConfigured("PRIVATE_MEDIA_ROOT must not overlap STATIC_ROOT.")
PUBLIC_BASE_URL = validate_public_base_url(PUBLIC_BASE_URL, require_https=True)

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
