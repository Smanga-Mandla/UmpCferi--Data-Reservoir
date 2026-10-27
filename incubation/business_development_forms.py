from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model

from accounts.access import accessible_queryset
from accounts.models import RoleGrant
from documents.validators import validate_document_file
from incubation.models import (
    IncubateeBusinessDevelopmentNeed,
    IncubateeBusinessDevelopmentNeedUpdate,
    MentoringSession,
)
from incubation.support_forms import eligible_request_assignees
from interactions.models import Interaction

User = get_user_model()


class BusinessDevelopmentNeedsForm(forms.Form):
    categories = forms.MultipleChoiceField(
        choices=IncubateeBusinessDevelopmentNeed.Category.choices,
        widget=forms.CheckboxSelectMultiple,
        label="What support do you need?",
    )
    description = forms.CharField(
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
        label="Additional details",
    )
    evidence = forms.FileField(
        required=False,
        help_text="Optional. Files are privately stored (maximum 25 MB).",
    )
    create_support_requests = forms.BooleanField(
        required=False,
        label="Also submit these needs through Support Requests for CFERI follow-up",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-check-input"
                if isinstance(field.widget, forms.CheckboxSelectMultiple)
                or isinstance(field.widget, forms.CheckboxInput)
                else "form-control"
            )

    def clean_categories(self):
        categories = self.cleaned_data["categories"]
        allowed = {value for value, _ in IncubateeBusinessDevelopmentNeed.Category.choices}
        if not categories or not set(categories).issubset(allowed):
            raise forms.ValidationError("Select at least one valid support need.")
        return categories

    def clean_evidence(self):
        evidence = self.cleaned_data.get("evidence")
        if evidence:
            self.cleaned_data["detected_content_type"] = validate_document_file(evidence)
        return evidence

    def clean(self):
        cleaned_data = super().clean()
        if (
            IncubateeBusinessDevelopmentNeed.Category.OTHER
            in cleaned_data.get("categories", [])
            and not cleaned_data.get("description", "").strip()
        ):
            self.add_error("description", "Describe the other support need.")
        return cleaned_data

    @staticmethod
    def safe_filename(uploaded_file):
        return Path(uploaded_file.name.replace("\\", "/")).name[:255]


class BusinessDevelopmentNeedUpdateForm(forms.ModelForm):
    class Meta:
        model = IncubateeBusinessDevelopmentNeedUpdate
        fields = (
            "status",
            "assigned_to",
            "support_provided",
            "outcome",
            "resolution_date",
            "evidence",
            "mentoring_session",
            "interaction",
        )
        widgets = {
            "support_provided": forms.Textarea(attrs={"rows": 3}),
            "outcome": forms.Textarea(attrs={"rows": 3}),
            "resolution_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, business, manager, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = eligible_request_assignees(
            business, manager
        )
        self.fields["assigned_to"].required = False
        self.fields["mentoring_session"].queryset = MentoringSession.objects.filter(
            incubatee=business
        ).order_by("-session_date", "-pk")
        self.fields["mentoring_session"].required = False
        self.fields["interaction"].queryset = accessible_queryset(
            manager,
            RoleGrant.Resource.INTERACTIONS,
            RoleGrant.Action.READ,
            Interaction.objects.filter(incubatees=business, is_deleted=False),
        ).distinct().order_by("-occurred_at", "-pk")
        self.fields["interaction"].required = False
        self.fields["evidence"].required = False
        self.fields["evidence"].help_text = (
            "Optional. Files are privately stored (maximum 25 MB)."
        )
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )

    def clean_evidence(self):
        evidence = self.cleaned_data.get("evidence")
        if evidence:
            self.cleaned_data["detected_content_type"] = validate_document_file(evidence)
        return evidence

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("status") == IncubateeBusinessDevelopmentNeed.Status.RESOLVED
            and self.initial.get("status")
            != IncubateeBusinessDevelopmentNeed.Status.RESOLVED
        ):
            if not cleaned_data.get("support_provided", "").strip():
                self.add_error("support_provided", "Record the assistance provided.")
            if not cleaned_data.get("outcome", "").strip():
                self.add_error("outcome", "Record the outcome achieved.")
        return cleaned_data

    @staticmethod
    def safe_filename(uploaded_file):
        return Path(uploaded_file.name.replace("\\", "/")).name[:255]
