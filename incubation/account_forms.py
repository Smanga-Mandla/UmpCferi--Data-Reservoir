from django import forms
from django.contrib.auth import get_user_model

from accounts.validators import validate_gmail_email
from incubation.models import Incubatee, IncubateeAccount

User = get_user_model()


class IncubateeAccountForm(forms.Form):
    full_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "name"}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": "form-control", "autocomplete": "email"})
    )
    businesses = forms.ModelMultipleChoiceField(
        queryset=Incubatee.objects.none(),
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": "6"}),
        help_text="Select each existing business for which this account is explicitly authorized.",
    )
    entrepreneur_category = forms.ChoiceField(
        choices=IncubateeAccount.EntrepreneurCategory.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    account_status = forms.ChoiceField(
        choices=(("active", "Active"), ("suspended", "Suspended")),
        initial="active",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, business_queryset, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["businesses"].queryset = business_queryset.order_by("legal_name")

    def clean_full_name(self):
        full_name = " ".join(self.cleaned_data["full_name"].split())
        name_parts = full_name.split(maxsplit=1)
        if len(name_parts) == 2 and (len(name_parts[0]) > 150 or len(name_parts[1]) > 150):
            raise forms.ValidationError("Each name field must be 150 characters or fewer.")
        return full_name

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"].strip()).lower()
        try:
            validate_gmail_email(email)
        except forms.ValidationError as error:
            raise forms.ValidationError(error.messages) from error
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        full_name = cleaned_data.get("full_name", "")
        if full_name and len(full_name.split()) < 2:
            self.add_error("full_name", "Enter both a first name and a surname.")
        if not cleaned_data.get("businesses"):
            self.add_error("businesses", "Link the account to at least one existing business.")
        return cleaned_data


class IncubateeAccountSearchForm(forms.Form):
    q = forms.CharField(
        required=False,
        max_length=100,
        label="Search incubatee accounts",
        widget=forms.SearchInput(attrs={"class": "form-control", "placeholder": "Name, email, or business"}),
    )
