import os
from pathlib import Path

from dotenv import load_dotenv

_base_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_base_dir / ".env", override=False)
os.environ.setdefault("DJANGO_DEBUG", "true")

from config.settings.base import *  # noqa: F403,E402

DEBUG = True
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend"
)
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
