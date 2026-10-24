from django.core.exceptions import ValidationError


def validate_gmail_email(value):
    if not value.strip().lower().endswith("@gmail.com"):
        raise ValidationError(
            "Use a Gmail address ending in @gmail.com.",
            code="invalid_gmail_email",
        )


def validate_ump_email(value):
    if not value.strip().lower().endswith("@ump.ac.za"):
        raise ValidationError(
            "Use a University of Mpumalanga email address ending in @ump.ac.za.",
            code="invalid_ump_email",
        )
