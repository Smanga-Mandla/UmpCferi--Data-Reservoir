from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


def validate_public_base_url(value, *, require_https=False):
    parsed = urlsplit(value)
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        expected = "an HTTPS URL" if require_https else "an HTTP(S) URL"
        raise ImproperlyConfigured(
            f"DJANGO_PUBLIC_BASE_URL must be {expected} without credentials, query, or fragment."
        )
    return value.rstrip("/")
