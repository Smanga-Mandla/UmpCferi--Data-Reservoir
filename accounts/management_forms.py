from django import forms
from django.contrib.auth import get_user_model

from accounts.models import Role, RoleGrant
from accounts.validators import validate_gmail_email

User = get_user_model()


class UserInvitationForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"class": "form-control"}))
    first_name = forms.CharField(required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    last_name = forms.CharField(required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    roles = forms.ModelMultipleChoiceField(
        queryset=Role.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    access_expires_at = forms.DateTimeField(
        required=False,
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(
            attrs={"class": "form-control", "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        roles = Role.objects.order_by("code")
        if actor is not None and not _may_assign_super_admin(actor):
            roles = roles.exclude(code=Role.Code.SUPER_ADMIN)
        self.fields["roles"].queryset = roles

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"].strip())
        try:
            validate_gmail_email(email)
        except forms.ValidationError as error:
            raise forms.ValidationError(error.messages) from error
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        roles = cleaned_data.get("roles")
        role_codes = set(roles.values_list("code", flat=True)) if roles is not None else set()
        if (
            Role.Code.SUPER_ADMIN in role_codes
            and self.actor is not None
            and not _may_assign_super_admin(self.actor)
        ):
            self.add_error("roles", "Only a Super Admin can assign the Super Admin role.")
        if role_codes & {Role.Code.FREELANCER, Role.Code.EXTERNAL_GUEST}:
            if not cleaned_data.get("access_expires_at"):
                self.add_error(
                    "access_expires_at",
                    "An expiry date is required for Freelancer and External Guest accounts.",
                )
        return cleaned_data


class UserAccessForm(forms.Form):
    roles = forms.ModelMultipleChoiceField(
        queryset=Role.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    access_expires_at = forms.DateTimeField(
        required=False,
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(
            attrs={"class": "form-control", "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
    )

    def __init__(self, *args, target_user, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_user = target_user
        self.actor = actor
        roles = Role.objects.order_by("code")
        if actor is not None and not _may_assign_super_admin(actor):
            roles = roles.exclude(code=Role.Code.SUPER_ADMIN)
        self.fields["roles"].queryset = roles
        if not self.is_bound:
            self.initial["roles"] = target_user.roles.all()
            if target_user.access_expires_at:
                self.initial["access_expires_at"] = target_user.access_expires_at.strftime(
                    "%Y-%m-%dT%H:%M"
                )

    def clean(self):
        cleaned_data = super().clean()
        roles = cleaned_data.get("roles")
        role_codes = set(roles.values_list("code", flat=True)) if roles is not None else set()
        if (
            Role.Code.SUPER_ADMIN in role_codes
            and self.actor is not None
            and not _may_assign_super_admin(self.actor)
        ):
            self.add_error("roles", "Only a Super Admin can assign the Super Admin role.")
        if role_codes & {Role.Code.FREELANCER, Role.Code.EXTERNAL_GUEST}:
            if not cleaned_data.get("access_expires_at"):
                self.add_error(
                    "access_expires_at",
                    "An expiry date is required for Freelancer and External Guest accounts.",
                )
        return cleaned_data


def _may_assign_super_admin(actor):
    return actor.is_superuser or actor.roles.filter(code=Role.Code.SUPER_ADMIN).exists()


class RoleGrantMatrixForm(forms.Form):
    NONE = "none"

    def __init__(self, *args, role, **kwargs):
        super().__init__(*args, **kwargs)
        self.role = role
        scopes = [(self.NONE, "No access"), *RoleGrant.Scope.choices]
        existing = {
            (grant.resource, grant.action): grant.scope
            for grant in role.grants.all()
        }
        for resource, _resource_label in RoleGrant.Resource.choices:
            for action, _action_label in RoleGrant.Action.choices:
                name = f"{resource}__{action}"
                self.fields[name] = forms.ChoiceField(
                    choices=scopes,
                    label=f"{resource} — {action}",
                    widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
                    initial=existing.get((resource, action), self.NONE),
                )

    def save(self):
        RoleGrant.objects.filter(role=self.role).delete()
        RoleGrant.objects.bulk_create(
            [
                RoleGrant(
                    role=self.role,
                    resource=resource,
                    action=action,
                    scope=self.cleaned_data[f"{resource}__{action}"],
                )
                for resource, _resource_label in RoleGrant.Resource.choices
                for action, _action_label in RoleGrant.Action.choices
                if self.cleaned_data[f"{resource}__{action}"] != self.NONE
            ]
        )
