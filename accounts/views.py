import secrets

import pyotp
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from accounts.forms import (
    EmailAuthenticationForm,
    UserPasswordResetForm,
    clear_failed_logins,
    lockout_status,
    record_failed_login,
)
from accounts.management_forms import RoleGrantMatrixForm, UserAccessForm, UserInvitationForm
from accounts.mfa import decrypt_totp_secret, encrypt_totp_secret, verify_totp
from accounts.models import Role, RoleGrant, TOTPDevice


class EmailLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = EmailAuthenticationForm

    def get_success_url(self):
        if self.request.user.roles.filter(code=Role.Code.INCUBATEE).exists():
            return reverse("incubation:incubatee_dashboard")
        return super().get_success_url()

    def form_valid(self, form):
        user = form.get_user()
        if user.has_mandatory_mfa_role:
            devices = user.totp_devices.filter(confirmed=True)
            if devices.exists():
                token = form.cleaned_data.get("otp_token", "")
                valid_device = next(
                    (device for device in devices if verify_totp(device, token)),
                    None,
                )
                if valid_device is None:
                    record_failed_login(user.email)
                    form.add_error("otp_token", "Enter a valid authenticator code.")
                    return self.form_invalid(form)
                valid_device.last_used_at = timezone.now()
                valid_device.save(update_fields=("last_used_at",))

        clear_failed_logins(user.email)
        response = super().form_valid(form)
        if user.has_mandatory_mfa_role:
            self.request.session["mfa_verified_user"] = user.pk if devices.exists() else None
            if not devices.exists():
                return redirect("accounts:mfa_setup")
        return response


class UserLogoutView(LogoutView):
    pass


def _has_user_admin_role(user):
    return user.is_authenticated and not user.roles.filter(
        code=Role.Code.INCUBATEE
    ).exists() and (
        user.is_superuser
        or user.roles.filter(
            code__in=(Role.Code.SUPER_ADMIN, Role.Code.SYSTEM_ADMIN)
        ).exists()
    )


def _is_super_admin(user):
    return user.is_authenticated and not user.roles.filter(
        code=Role.Code.INCUBATEE
    ).exists() and (
        user.is_superuser or user.roles.filter(code=Role.Code.SUPER_ADMIN).exists()
    )


def _require_current_mfa(request):
    if not request.user.has_mandatory_mfa_role:
        raise PermissionDenied
    return True


@require_http_methods(["GET", "POST"])
def mfa_setup(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    _require_current_mfa(request)
    if request.user.totp_devices.filter(confirmed=True).exists():
        return redirect("accounts:mfa_verify")
    if lockout_status(request.user.email):
        from django.contrib.auth import logout

        logout(request)
        return redirect("accounts:login")

    device, _created = TOTPDevice.objects.get_or_create(
        user=request.user,
        confirmed=False,
        defaults={"secret_encrypted": encrypt_totp_secret(pyotp.random_base32())},
    )
    secret = decrypt_totp_secret(device.secret_encrypted)
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(
        name=request.user.email,
        issuer_name="UMP CFERI Data Reservoir",
    )

    if request.method == "POST":
        token = request.POST.get("otp_token", "")
        if verify_totp(device, token):
            device.confirmed = True
            device.save(update_fields=("confirmed",))
            clear_failed_logins(request.user.email)
            request.session["mfa_verified_user"] = request.user.pk
            messages.success(request, "Two-factor authentication is now enabled.")
            return redirect("reporting:dashboard")
        record_failed_login(request.user.email)
        if lockout_status(request.user.email):
            logout(request)
            return redirect("accounts:login")
        messages.error(request, "The authenticator code was not valid. Try again.")

    return render(
        request,
        "accounts/mfa_setup.html",
        {"secret": secret, "provisioning_uri": provisioning_uri},
    )


@require_http_methods(["GET", "POST"])
def mfa_verify(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    _require_current_mfa(request)
    if request.session.get("mfa_verified_user") == request.user.pk:
        return redirect("reporting:dashboard")
    if lockout_status(request.user.email):
        logout(request)
        return redirect("accounts:login")
    devices = request.user.totp_devices.filter(confirmed=True)
    if not devices.exists():
        return redirect("accounts:mfa_setup")
    if request.method == "POST":
        token = request.POST.get("otp_token", "")
        device = next((item for item in devices if verify_totp(item, token)), None)
        if device:
            clear_failed_logins(request.user.email)
            request.session["mfa_verified_user"] = request.user.pk
            return redirect("reporting:dashboard")
        record_failed_login(request.user.email)
        if lockout_status(request.user.email):
            logout(request)
            return redirect("accounts:login")
        messages.error(request, "The authenticator code was not valid. Try again.")
    return render(request, "accounts/mfa_verify.html")


@user_passes_test(_has_user_admin_role, login_url="accounts:login")
def manage_users(request):
    users = get_user_model().objects.prefetch_related("roles").order_by("email")
    return render(request, "accounts/manage_users.html", {"managed_users": users})


@require_http_methods(["GET", "POST"])
def invite_user(request):
    if not _has_user_admin_role(request.user):
        raise PermissionDenied
    form = UserInvitationForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        temporary_password = secrets.token_urlsafe(48)
        with transaction.atomic():
            user = get_user_model().objects.create_user(
                email=data["email"],
                password=temporary_password,
                first_name=data["first_name"],
                last_name=data["last_name"],
                access_expires_at=data["access_expires_at"],
            )
            user.roles.set(data["roles"])
            reset_form = UserPasswordResetForm({"email": user.email})
            if reset_form.is_valid():
                reset_form.save(
                    request=request,
                    use_https=request.is_secure(),
                    token_generator=default_token_generator,
                    email_template_name="accounts/invitation_email.html",
                    subject_template_name="accounts/invitation_subject.txt",
                )
        messages.success(request, f"Invitation sent to {user.email}.")
        return redirect("accounts:manage_users")
    return render(request, "accounts/user_form.html", {"form": form, "title": "Invite user"})


@require_http_methods(["GET", "POST"])
def edit_user_access(request, user_id):
    if not _has_user_admin_role(request.user):
        raise PermissionDenied
    target = get_object_or_404(get_user_model(), pk=user_id)
    if target.roles.filter(code=Role.Code.INCUBATEE).exists():
        raise PermissionDenied("Manage incubatee accounts through Manage Incubatees.")
    if target.pk == request.user.pk:
        raise PermissionDenied("You cannot change your own roles or access expiry.")
    if (
        (target.is_superuser or target.roles.filter(code=Role.Code.SUPER_ADMIN).exists())
        and not _is_super_admin(request.user)
    ):
        raise PermissionDenied("Only a Super Admin can manage a Super Admin account.")

    form = UserAccessForm(
        request.POST or None,
        target_user=target,
        actor=request.user,
        initial={"access_expires_at": target.access_expires_at},
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            target.access_expires_at = form.cleaned_data["access_expires_at"]
            target.save(update_fields=("access_expires_at",))
            target.roles.set(form.cleaned_data["roles"])
        messages.success(request, f"Access updated for {target.email}.")
        return redirect("accounts:manage_users")
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "title": f"Manage access for {target.email}", "target": target},
    )


@require_POST
def deactivate_user(request, user_id):
    if not _has_user_admin_role(request.user):
        raise PermissionDenied
    target = get_object_or_404(get_user_model(), pk=user_id)
    if target.roles.filter(code=Role.Code.INCUBATEE).exists():
        raise PermissionDenied("Manage incubatee accounts through Manage Incubatees.")
    if target.pk == request.user.pk:
        raise PermissionDenied("You cannot deactivate your own account.")
    if (
        (target.is_superuser or target.roles.filter(code=Role.Code.SUPER_ADMIN).exists())
        and not _is_super_admin(request.user)
    ):
        raise PermissionDenied("Only a Super Admin can deactivate a Super Admin account.")
    target.is_active = False
    target.save(update_fields=("is_active",))
    messages.success(request, f"Deactivated {target.email}.")
    return redirect("accounts:manage_users")


class UserPasswordResetView(PasswordResetView):
    template_name = "accounts/password_reset_form.html"
    email_template_name = "accounts/password_reset_email.html"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")
    form_class = UserPasswordResetForm


class UserPasswordResetDoneView(PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class UserPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    form_class = SetPasswordForm
    success_url = reverse_lazy("accounts:password_reset_complete")

    def form_valid(self, form):
        response = super().form_valid(form)
        account = getattr(form.user, "incubatee_account", None)
        if account and account.invitation_accepted_at is None:
            account.invitation_accepted_at = timezone.now()
            account.save(update_fields=("invitation_accepted_at",))
        return response


class UserPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"


@user_passes_test(_is_super_admin, login_url="accounts:login")
def role_matrix(request):
    roles = Role.objects.exclude(code=Role.Code.INCUBATEE).order_by("code")
    role_code = request.POST.get("role") if request.method == "POST" else request.GET.get("role")
    selected_role = get_object_or_404(roles, code=role_code) if role_code else roles.first()
    if selected_role is None:
        raise PermissionDenied("The role matrix has not been initialized.")
    form = RoleGrantMatrixForm(
        request.POST or None,
        role=selected_role,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
        messages.success(request, f"Permissions updated for {selected_role.get_code_display()}.")
        return redirect(f"{reverse_lazy('accounts:role_matrix')}?role={selected_role.code}")
    matrix_rows = [
        {
            "resource_label": resource_label,
            "action_label": action_label,
            "field": form[f"{resource}__{action}"],
        }
        for resource, resource_label in RoleGrant.Resource.choices
        for action, action_label in RoleGrant.Action.choices
    ]
    return render(
        request,
        "accounts/role_matrix.html",
        {
            "roles": roles,
            "selected_role": selected_role,
            "resources": RoleGrant.Resource.choices,
            "actions": RoleGrant.Action.choices,
            "form": form,
            "matrix_rows": matrix_rows,
        },
    )
