from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import LoginThrottle


def lockout_status(email):
    try:
        throttle = LoginThrottle.objects.get(email__iexact=email)
    except LoginThrottle.DoesNotExist:
        return False
    if throttle.locked_until and throttle.locked_until > timezone.now():
        return True
    if throttle.locked_until:
        throttle.failed_attempts = 0
        throttle.locked_until = None
        throttle.save(update_fields=("failed_attempts", "locked_until", "updated_at"))
    return False


def record_failed_login(email):
    normalized_email = email.strip().lower()
    with transaction.atomic():
        LoginThrottle.objects.get_or_create(email=normalized_email)
        throttle = LoginThrottle.objects.select_for_update().get(email=normalized_email)
        now = timezone.now()
        if throttle.locked_until and throttle.locked_until > now:
            return throttle
        if throttle.locked_until:
            throttle.failed_attempts = 0
            throttle.locked_until = None
        throttle.failed_attempts += 1
        if throttle.failed_attempts >= 5:
            throttle.locked_until = now + timedelta(minutes=15)
        throttle.save(update_fields=("failed_attempts", "locked_until", "updated_at"))
        return throttle


def clear_failed_logins(email):
    LoginThrottle.objects.filter(email__iexact=email).delete()


class UserPasswordResetForm(PasswordResetForm):
    def get_users(self, email):
        yield from get_user_model()._default_manager.filter(
            email__iexact=email,
            is_active=True,
        )


class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "autocomplete": "email",
                "autofocus": True,
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        min_length=8,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "autocomplete": "current-password",
                "minlength": 8,
            }
        ),
    )
    otp_token = forms.CharField(
        label="Authenticator code",
        required=False,
        min_length=6,
        max_length=6,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
            }
        ),
    )

    def clean_username(self):
        email = self.cleaned_data["username"].strip().lower()
        if not email.endswith("@gmail.com"):
            raise ValidationError("Use your @gmail.com email address.")
        return email

    def clean(self):
        email = self.cleaned_data.get("username", "")
        if email and lockout_status(email):
            raise ValidationError("The email or password was not recognized.")
        try:
            cleaned_data = super().clean()
        except ValidationError:
            if email:
                record_failed_login(email)
            raise
        return cleaned_data
