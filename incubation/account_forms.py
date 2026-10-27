from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from accounts.models import Role
from accounts.validators import validate_gmail_email
from incubation.account_access import assignable_cferi_staff
from incubation.models import Incubatee, IncubateeAccount, Programme

User = get_user_model()


class IncubateeAccountForm(forms.Form):
    class RegistrationMode:
        NEW_BUSINESS = "new_business"
        EXISTING_BUSINESS = "existing_business"

    registration_mode = forms.ChoiceField(
        choices=(
            (RegistrationMode.NEW_BUSINESS, "Register new incubatee and business"),
            (RegistrationMode.EXISTING_BUSINESS, "Link incubatee to existing business"),
        ),
        required=False,
        initial=RegistrationMode.EXISTING_BUSINESS,
        widget=forms.RadioSelect,
        label="Registration option",
    )
    full_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "name"}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": "form-control", "autocomplete": "email"})
    )
    businesses = forms.ModelMultipleChoiceField(
        queryset=Incubatee.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": "6"}),
        help_text="Select the existing business or businesses this account may access.",
        label="Existing business",
    )
    entrepreneur_category = forms.ChoiceField(
        choices=IncubateeAccount.EntrepreneurCategory.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    new_business_name = forms.CharField(
        max_length=255,
        required=False,
        label="Business name or proposed business name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    business_description = forms.CharField(
        required=False,
        label="Business description",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
    business_registration_status = forms.ChoiceField(
        choices=(
            choice
            for choice in Incubatee.RegistrationStatus.choices
            if choice[0] != Incubatee.RegistrationStatus.UNKNOWN
        ),
        required=False,
        label="Business registration status",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    cipc_registration_number = forms.CharField(
        required=False,
        max_length=128,
        label="CIPC registration number (optional)",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    date_registered = forms.DateField(
        required=False,
        label="Business registration date (optional)",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    business_stage = forms.ChoiceField(
        choices=Incubatee.BusinessStage.choices,
        required=False,
        label="Business stage",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    programme = forms.ModelChoiceField(
        queryset=Programme.objects.none(),
        required=False,
        empty_label="No programme selected",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    assigned_staff = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="Choose CFERI Staff member",
        label="Assigned CFERI Staff member",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    initial_support_needs = forms.CharField(
        required=False,
        label="Initial support needs",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
    account_status = forms.ChoiceField(
        choices=(("active", "Active"), ("suspended", "Suspended")),
        initial="active",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(
        self, *args, business_queryset, business_query="", actor=None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.fields["businesses"].queryset = business_queryset.order_by("legal_name")
        self.fields["programme"].queryset = Programme.objects.filter(
            is_active=True
        ).order_by("name")
        self.fields["assigned_staff"].queryset = assignable_cferi_staff()
        if actor and actor.roles.filter(code=Role.Code.CFERI_STAFF).exists():
            self.fields["assigned_staff"].initial = actor.pk
        self.business_query = " ".join(business_query.split())
        if self.business_query:
            self.fields["businesses"].queryset = self.fields[
                "businesses"
            ].queryset.filter(
                Q(legal_name__icontains=self.business_query)
                | Q(trading_name__icontains=self.business_query)
            )

    def clean_registration_mode(self):
        return (
            self.cleaned_data.get("registration_mode")
            or self.RegistrationMode.EXISTING_BUSINESS
        )

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

    def clean_new_business_name(self):
        name = " ".join(self.cleaned_data.get("new_business_name", "").split())
        if (
            self.cleaned_data.get("registration_mode")
            == self.RegistrationMode.NEW_BUSINESS
            and name
            and Incubatee.all_objects.filter(
                Q(legal_name__iexact=name) | Q(trading_name__iexact=name)
            ).exists()
        ):
            raise forms.ValidationError(
                "A business with this name already exists. Search for and link the existing business."
            )
        return name

    def clean_cipc_registration_number(self):
        number = (self.cleaned_data.get("cipc_registration_number") or "").strip()
        if (
            self.cleaned_data.get("registration_mode")
            == self.RegistrationMode.NEW_BUSINESS
            and number
            and Incubatee.all_objects.filter(
                cipc_registration_number__iexact=number
            ).exists()
        ):
            raise forms.ValidationError(
                "This CIPC registration number already belongs to an existing business."
            )
        return number or None

    def clean(self):
        cleaned_data = super().clean()
        full_name = cleaned_data.get("full_name", "")
        if full_name and len(full_name.split()) < 2:
            self.add_error("full_name", "Enter both a first name and a surname.")

        if cleaned_data.get("registration_mode") == self.RegistrationMode.NEW_BUSINESS:
            for field in (
                "new_business_name",
                "business_description",
                "business_registration_status",
                "business_stage",
                "assigned_staff",
            ):
                if not cleaned_data.get(field):
                    self.add_error(field, "This field is required when registering a new business.")
        elif not cleaned_data.get("businesses"):
            self.add_error("businesses", "Link the account to at least one existing business.")
        return cleaned_data


class IncubateeAccountSearchForm(forms.Form):
    q = forms.CharField(
        required=False,
        max_length=100,
        label="Search incubatee accounts",
        widget=forms.SearchInput(attrs={"class": "form-control", "placeholder": "Name, email, or business"}),
    )
