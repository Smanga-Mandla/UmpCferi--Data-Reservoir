from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError

from documents.validators import validate_document_file
from incubation.models import BusinessDocument, Incubatee, IncubateeProfileChange


class BusinessProfileForm(forms.ModelForm):
    class Meta:
        model = Incubatee
        fields = (
            "trading_name",
            "sic_code",
            "sub_sector",
            "products_services",
            "business_model",
            "business_stage",
            "physical_address",
            "province",
            "municipality",
            "business_contact_email",
            "business_contact_number",
        )
        labels = {
            "sic_code": "Business sector / SIC code",
            "sub_sector": "Sub-sector",
            "products_services": "Products and services",
            "business_contact_email": "Business contact email",
            "business_contact_number": "Business contact number",
        }
        widgets = {
            "products_services": forms.Textarea(attrs={"rows": 4}),
            "physical_address": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )

    def clean_business_contact_number(self):
        number = self.cleaned_data["business_contact_number"].strip()
        if number and any(character not in "+0123456789().- " for character in number):
            raise ValidationError("Enter a valid business contact number.")
        return number


class ProtectedProfileChangeForm(forms.Form):
    legal_name = forms.CharField(max_length=255, required=False)
    cipc_registration_number = forms.CharField(max_length=128, required=False)
    entity_type = forms.ChoiceField(
        choices=(("", "---------"), *Incubatee.EntityType.choices),
        required=False,
    )
    date_registered = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, incubatee, **kwargs):
        self.incubatee = incubatee
        super().__init__(*args, **kwargs)
        self.fields["legal_name"].initial = incubatee.legal_name
        self.fields["cipc_registration_number"].initial = incubatee.cipc_registration_number
        self.fields["entity_type"].initial = incubatee.entity_type
        self.fields["date_registered"].initial = incubatee.date_registered
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )

    def clean_cipc_registration_number(self):
        number = self.cleaned_data["cipc_registration_number"].strip() or None
        if number and Incubatee.objects.filter(
            cipc_registration_number__iexact=number
        ).exclude(pk=self.incubatee.pk).exists():
            raise ValidationError("This registration number is already recorded for another business.")
        return number

    def clean_legal_name(self):
        value = self.cleaned_data["legal_name"].strip()
        if not value:
            raise ValidationError("Legal business name cannot be blank.")
        return value

    def clean(self):
        cleaned_data = super().clean()
        for field in ("cipc_registration_number", "entity_type", "date_registered"):
            if field not in self.data:
                cleaned_data[field] = getattr(self.incubatee, field)
        return cleaned_data

    def proposed_values(self):
        if not hasattr(self, "cleaned_data"):
            raise ValueError("The form must be valid before reading proposed values.")
        fields = ("legal_name", "cipc_registration_number", "entity_type", "date_registered")
        return {
            field: (
                self.cleaned_data[field].isoformat()
                if self.cleaned_data[field] is not None
                and field == "date_registered"
                else self.cleaned_data[field]
            )
            for field in fields
            if self.cleaned_data[field] != getattr(self.incubatee, field)
        }


class BusinessDocumentForm(forms.ModelForm):
    file = forms.FileField()
    change_note = forms.CharField(max_length=500, required=False)

    class Meta:
        model = BusinessDocument
        fields = ("title", "category", "description", "expiry_date")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "expiry_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = "form-control"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        self.cleaned_data["detected_content_type"] = validate_document_file(uploaded_file)
        return uploaded_file

    @staticmethod
    def safe_filename(uploaded_file):
        return Path(uploaded_file.name.replace("\\", "/")).name[:255]

    def metadata(self):
        return {
            field: self.cleaned_data[field]
            for field in ("title", "category", "description", "expiry_date")
        }
