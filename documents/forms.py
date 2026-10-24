from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model

from documents.models import Document
from documents.validators import validate_document_file

User = get_user_model()


class DocumentForm(forms.Form):
    title = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"class": "form-control"}))
    category = forms.ChoiceField(
        choices=Document.Category.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reference_number = forms.CharField(
        max_length=128,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    issuing_authority = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    issue_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    expiry_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    sensitivity = forms.ChoiceField(
        choices=Document.Sensitivity.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    owner = forms.ModelChoiceField(
        queryset=User.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
    tags = forms.CharField(
        required=False,
        help_text="Separate tags with commas.",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    file = forms.FileField(
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
    )
    change_note = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, initial_owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = User.objects.filter(is_active=True).order_by("email")
        if initial_owner is not None and not self.is_bound:
            self.fields["owner"].initial = initial_owner

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        self.cleaned_data["detected_content_type"] = validate_document_file(uploaded_file)
        return uploaded_file

    def clean_tags(self):
        raw_tags = self.cleaned_data["tags"]
        return list(dict.fromkeys(tag.strip() for tag in raw_tags.split(",") if tag.strip()))

    def clean(self):
        cleaned_data = super().clean()
        issue_date = cleaned_data.get("issue_date")
        expiry_date = cleaned_data.get("expiry_date")
        if issue_date and expiry_date and expiry_date < issue_date:
            self.add_error("expiry_date", "Expiry date cannot be before the issue date.")
        return cleaned_data

    def metadata(self):
        data = self.cleaned_data.copy()
        data.pop("file", None)
        data.pop("change_note", None)
        data.pop("detected_content_type", None)
        data["owner_id"] = data.pop("owner").pk
        return data

    @staticmethod
    def safe_original_filename(uploaded_file):
        return Path(uploaded_file.name).name[:255]
