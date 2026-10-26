from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from accounts.models import Role
from documents.validators import validate_document_file
from incubation.models import IncubateeSupportRequest, IncubateeSupportRequestUpdate

User = get_user_model()


class IncubateeSupportRequestForm(forms.ModelForm):
    class Meta:
        model = IncubateeSupportRequest
        fields = ("category", "description", "priority", "evidence")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "priority": forms.Select(),
        }
        help_texts = {
            "priority": "Optional; prioritization is subject to CFERI review.",
            "evidence": "Optional. Files are privately stored (maximum 25 MB).",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["priority"].required = False
        self.fields["evidence"].required = False
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )

    def clean_evidence(self):
        evidence = self.cleaned_data.get("evidence")
        if evidence:
            self.cleaned_data["detected_content_type"] = validate_document_file(evidence)
        return evidence

    @staticmethod
    def safe_filename(uploaded_file):
        return Path(uploaded_file.name.replace("\\", "/")).name[:255]


class SupportRequestReviewForm(forms.Form):
    status = forms.ChoiceField(choices=IncubateeSupportRequest.Status.choices)
    assigned_to = forms.ModelChoiceField(queryset=User.objects.none(), required=False)
    response = forms.CharField(
        required=True,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, eligible_staff=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = (
            eligible_staff
            if eligible_staff is not None
            else User.objects.none()
        )
        self.fields["assigned_to"].label_from_instance = (
            lambda user: user.get_full_name() or user.email
        )
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )


def eligible_request_assignees(business, manager):
    return (
        User.objects.filter(
            Q(roles__code=Role.Code.ADMIN)
            | Q(
                roles__code=Role.Code.CFERI_STAFF,
                pk__in=business.assigned_users.values("pk"),
            )
            | Q(pk=manager.pk),
            is_active=True,
        )
        .distinct()
        .order_by("last_name", "first_name", "email")
    )
