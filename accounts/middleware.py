from datetime import timedelta

from django.contrib.auth import logout
from django.conf import settings
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.utils import timezone


class AccessControlMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and user.is_access_expired:
            user.is_active = False
            user.save(update_fields=("is_active",))
            logout(request)
        return self.get_response(request)


class InactivityTimeoutMiddleware:
    session_key = "_auth_last_activity"
    timeout = timedelta(minutes=30)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            now = timezone.now()
            last_activity = request.session.get(self.session_key)
            if last_activity is not None:
                try:
                    elapsed = now - timezone.datetime.fromtimestamp(
                        float(last_activity),
                        tz=timezone.get_current_timezone(),
                    )
                except (TypeError, ValueError, OverflowError):
                    elapsed = self.timeout
                if elapsed >= self.timeout:
                    logout(request)
                    return redirect("accounts:login")
            request.session[self.session_key] = now.timestamp()
        return self.get_response(request)


class MfaEnforcementMiddleware:
    allowed_url_names = {"login", "logout", "mfa_setup", "mfa_verify"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and user.has_mandatory_mfa_role:
            try:
                route_name = resolve(request.path_info).url_name
            except Resolver404:
                route_name = None
            if request.path_info.startswith(settings.STATIC_URL):
                return self.get_response(request)
            has_device = user.totp_devices.filter(confirmed=True).exists()
            verified_user_id = request.session.get("mfa_verified_user")
            if not has_device:
                if route_name not in {"mfa_setup", "logout"}:
                    return redirect("accounts:mfa_setup")
            elif verified_user_id != user.pk:
                if route_name not in {"mfa_verify", "logout"}:
                    return redirect("accounts:mfa_verify")
        return self.get_response(request)
