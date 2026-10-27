from decimal import Decimal
from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.forms import formset_factory

from accounts.access import accessible_queryset
from accounts.models import RoleGrant
from documents.validators import validate_document_file
from incubation.account_access import assignable_cferi_staff
from incubation.models import (
    Cohort,
    CheckInAccessLink,
    FounderProfile,
    Incubatee,
    IncubateeConsent,
    IncubateeDocument,
    IncubateeExit,
    IncubateeFollowUp,
    IncubateeMetricObservation,
    IncubateeMilestone,
    IncubateeObservationEvidence,
    IncubateeFounder,
    MentoringSession,
    Programme,
    QuarterlyReview,
)
from stakeholders.models import Stakeholder

class BootstrapModelForm(forms.ModelForm):
    def apply_bootstrap(self):
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = "form-control"
            else:
                field.widget.attrs["class"] = "form-control"


class CompanyInformationForm(BootstrapModelForm):
    class Meta:
        model = Incubatee
        fields = (
            "legal_name",
            "trading_name",
            "cipc_registration_number",
            "date_registered",
            "entity_type",
            "vat_status",
            "tax_number",
            "bbbee_level",
            "physical_address",
            "province",
            "municipality",
            "business_contact_email",
            "business_contact_number",
            "urban_rural",
        )
        widgets = {
            "date_registered": forms.DateInput(attrs={"type": "date"}),
            "physical_address": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap()
        self.duplicate_incubatee = None
        self.potential_duplicates = []

    def clean_cipc_registration_number(self):
        number = (self.cleaned_data.get("cipc_registration_number") or "").strip()
        if not number:
            return None
        duplicate = Incubatee.objects.filter(
            cipc_registration_number__iexact=number
        ).exclude(pk=self.instance.pk or None).first()
        if duplicate:
            self.duplicate_incubatee = duplicate
            raise ValidationError(
                "This CIPC registration number is already registered to another incubatee."
            )
        return number

    def clean(self):
        cleaned_data = super().clean()
        legal_name = (cleaned_data.get("legal_name") or "").strip()
        if legal_name:
            duplicates = Incubatee.objects.filter(legal_name__iexact=legal_name).exclude(
                pk=self.instance.pk or None
            )
            self.potential_duplicates = list(duplicates[:5])
        return cleaned_data


class BusinessInformationForm(BootstrapModelForm):
    class Meta:
        model = Incubatee
        fields = (
            "sic_code",
            "sub_sector",
            "products_services",
            "business_model",
            "business_stage",
            "formality",
        )
        widgets = {"products_services": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sic_code"].help_text = (
            "Enter the applicable SIC code. Confirm the official code list/version before rollout."
        )
        self.apply_bootstrap()


class TeamForm(BootstrapModelForm):
    class Meta:
        model = Incubatee
        fields = (
            "full_time_employees",
            "part_time_employees",
            "full_time_men",
            "full_time_women",
            "full_time_youth",
            "part_time_men",
            "part_time_women",
            "part_time_youth",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap()


class FinancialBaselineForm(BootstrapModelForm):
    class Meta:
        model = Incubatee
        fields = (
            "revenue_12_months_zar",
            "monthly_average_revenue_zar",
            "funding_received_zar",
            "funding_sources",
            "debt_zar",
            "has_bank_account",
        )
        widgets = {
            "funding_sources": forms.Textarea(attrs={"rows": 3}),
            "revenue_12_months_zar": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
            "monthly_average_revenue_zar": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
            "funding_received_zar": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
            "debt_zar": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in (
            "revenue_12_months_zar",
            "monthly_average_revenue_zar",
            "funding_received_zar",
            "debt_zar",
        ):
            self.fields[field_name].validators.append(
                MinValueValidator(Decimal("0"))
            )
        self.apply_bootstrap()


class MarketForm(BootstrapModelForm):
    customer_type_1 = forms.CharField(max_length=255, required=False)
    customer_type_2 = forms.CharField(max_length=255, required=False)
    customer_type_3 = forms.CharField(max_length=255, required=False)

    class Meta:
        model = Incubatee
        fields = ("customer_count", "exports")
        widgets = {"customer_count": forms.NumberInput(attrs={"min": "0"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.update(
            {
                f"customer_type_{index}": (
                    self.instance.top_customer_types[index - 1]
                    if len(self.instance.top_customer_types) >= index
                    else ""
                )
                for index in range(1, 4)
            }
        )
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"

    def customer_types(self):
        return [
            self.cleaned_data.get(f"customer_type_{index}", "").strip()
            for index in range(1, 4)
            if self.cleaned_data.get(f"customer_type_{index}", "").strip()
        ]


class BusinessNeedsForm(BootstrapModelForm):
    support_need_1 = forms.CharField(max_length=255, required=False)
    support_need_2 = forms.CharField(max_length=255, required=False)
    support_need_3 = forms.CharField(max_length=255, required=False)

    class Meta:
        model = Incubatee
        fields = (
            "finance_score",
            "marketing_score",
            "operations_score",
            "compliance_score",
            "technology_score",
            "leadership_score",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in (
            "finance_score",
            "marketing_score",
            "operations_score",
            "compliance_score",
            "technology_score",
            "leadership_score",
        ):
            self.fields[name].validators.extend(
                (MinValueValidator(1), MaxValueValidator(5))
            )
            self.fields[name].widget = forms.Select(
                choices=[("", "Not provided"), *[(score, score) for score in range(1, 6)]],
                attrs={"class": "form-select"},
            )
        self.initial.update(
            {
                f"support_need_{index}": (
                    self.instance.top_support_needs[index - 1]
                    if len(self.instance.top_support_needs) >= index
                    else ""
                )
                for index in range(1, 4)
            }
        )
        for index in range(1, 4):
            self.fields[f"support_need_{index}"].widget.attrs["class"] = "form-control"

    def support_needs(self):
        return [
            self.cleaned_data.get(f"support_need_{index}", "").strip()
            for index in range(1, 4)
            if self.cleaned_data.get(f"support_need_{index}", "").strip()
        ]


class AssignmentForm(BootstrapModelForm):
    mentors = forms.ModelMultipleChoiceField(
        queryset=Stakeholder.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )

    class Meta:
        model = Incubatee
        fields = ("programme", "cohort", "staff_owner", "mentors")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["programme"].queryset = Programme.objects.filter(is_active=True)
        self.fields["cohort"].queryset = Cohort.objects.filter(is_active=True).select_related(
            "programme"
        )
        self.fields["staff_owner"].queryset = assignable_cferi_staff()
        self.fields["mentors"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            Stakeholder.objects.filter(is_deleted=False),
        ).order_by("name")
        self.fields["cohort"].required = False
        self.apply_bootstrap()

    def clean(self):
        cleaned_data = super().clean()
        cohort = cleaned_data.get("cohort")
        programme = cleaned_data.get("programme")
        if cohort and programme and cohort.programme_id != programme.pk:
            self.add_error("cohort", "Select a cohort belonging to the selected programme.")
        return cleaned_data


class LifecycleForm(BootstrapModelForm):
    class Meta:
        model = Incubatee
        fields = ("status",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap()


class FounderForm(forms.Form):
    founder_link_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    founder_name = forms.CharField(max_length=255, required=False)
    id_number = forms.CharField(max_length=64, required=False, widget=forms.PasswordInput(render_value=True))
    gender = forms.ChoiceField(choices=[("", "Not provided"), *FounderProfile.Gender.choices], required=False)
    date_of_birth = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    race = forms.ChoiceField(choices=[("", "Not provided"), *FounderProfile.Race.choices], required=False)
    disability_status = forms.ChoiceField(
        choices=[("", "Not provided"), *Incubatee.DisabilityStatus.choices], required=False
    )
    highest_qualification = forms.CharField(max_length=255, required=False)
    prior_business_experience = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    ownership_percentage = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        max_digits=5,
        decimal_places=2,
    )
    DELETE = forms.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"

    def clean(self):
        cleaned_data = super().clean()
        if any(
            cleaned_data.get(field)
            for field in (
                "id_number",
                "gender",
                "date_of_birth",
                "race",
                "disability_status",
                "highest_qualification",
                "prior_business_experience",
                "ownership_percentage",
            )
        ) and not cleaned_data.get("founder_name"):
            self.add_error("founder_name", "Enter a founder name for these details.")
        return cleaned_data


FounderFormSet = formset_factory(FounderForm, extra=1, max_num=10, validate_max=True)


class IncubateeConsentForm(forms.Form):
    popia_status = forms.ChoiceField(
        choices=[("", "Not recorded"), *Incubatee.ConsentStatus.choices], required=False
    )
    popia_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    popia_method = forms.CharField(max_length=255, required=False)
    funder_status = forms.ChoiceField(
        choices=[("", "Not recorded"), *Incubatee.ConsentStatus.choices], required=False
    )
    funder_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    funder_method = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, incubatee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.incubatee = incubatee
        if incubatee and not self.is_bound:
            for prefix, purpose in (
                ("popia", IncubateeConsent.Purpose.POPIA),
                ("funder", IncubateeConsent.Purpose.FUNDER_REPORTING),
            ):
                latest = incubatee.consent_history.filter(purpose=purpose).first()
                if latest:
                    self.initial[f"{prefix}_status"] = latest.status
                    self.initial[f"{prefix}_date"] = latest.consent_date
                    self.initial[f"{prefix}_method"] = latest.method
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"

    def clean(self):
        cleaned_data = super().clean()
        for prefix in ("popia", "funder"):
            if cleaned_data.get(f"{prefix}_status") and not cleaned_data.get(f"{prefix}_date"):
                self.add_error(f"{prefix}_date", "A date is required when recording a consent decision.")
        return cleaned_data


class IncubateeDocumentForm(forms.ModelForm):
    class Meta:
        model = IncubateeDocument
        fields = ("category", "title", "file")
        widgets = {
            "category": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "file": forms.ClearableFileInput(attrs={"class": "form-control"}),
        }

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        self.cleaned_data["detected_content_type"] = validate_document_file(uploaded_file)
        return uploaded_file

    @staticmethod
    def safe_filename(uploaded_file):
        return Path(uploaded_file.name).name[:255]


class IncubateeImportForm(forms.Form):
    workbook = forms.FileField(widget=forms.ClearableFileInput(attrs={"class": "form-control"}))

    def clean_workbook(self):
        uploaded = self.cleaned_data["workbook"]
        if Path(uploaded.name).suffix.lower() != ".xlsx":
            raise ValidationError("Upload an Excel .xlsx workbook.")
        if uploaded.size > 5 * 1024 * 1024:
            raise ValidationError("The workbook must not exceed 5 MB.")
        return uploaded


class ImportConfirmForm(forms.Form):
    pass


class StatusFilterForm(forms.Form):
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses"), *Incubatee.Status.choices],
        widget=forms.Select(attrs={"class": "form-select"}),
    )


MONTHLY_CHECKIN_FIELDS = (
    ("revenue_zar", IncubateeMetricObservation.Metric.REVENUE_ZAR, "Revenue this month (ZAR)", "decimal"),
    ("customer_count", IncubateeMetricObservation.Metric.CUSTOMER_COUNT, "Number of customers", "integer"),
    ("new_customers", IncubateeMetricObservation.Metric.NEW_CUSTOMERS, "New customers this month", "integer"),
    ("full_time_employees", IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES, "Full-time employees", "integer"),
    ("part_time_employees", IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES, "Part-time employees", "integer"),
    ("jobs_created", IncubateeMetricObservation.Metric.JOBS_CREATED, "Jobs created", "integer"),
    ("jobs_lost", IncubateeMetricObservation.Metric.JOBS_LOST, "Jobs lost", "integer"),
    ("funding_applied_zar", IncubateeMetricObservation.Metric.FUNDING_APPLIED_ZAR, "Funding applied for (ZAR)", "decimal"),
    ("funding_received_zar", IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR, "Funding received (ZAR)", "decimal"),
    ("funding_source", IncubateeMetricObservation.Metric.FUNDING_SOURCE, "Funding source(s)", "text"),
    ("biggest_win", IncubateeMetricObservation.Metric.BIGGEST_WIN, "Biggest win", "text"),
    ("biggest_challenge", IncubateeMetricObservation.Metric.BIGGEST_CHALLENGE, "Biggest challenge", "text"),
    ("challenge_category", IncubateeMetricObservation.Metric.CHALLENGE_CATEGORY, "Challenge category", "text"),
    ("support_needed", IncubateeMetricObservation.Metric.SUPPORT_NEEDED, "Support needed next month", "text"),
    ("confidence_score", IncubateeMetricObservation.Metric.CONFIDENCE_SCORE, "Confidence score (1–10)", "integer"),
)


class MonthlyCheckInForm(forms.Form):
    access_token = forms.CharField(widget=forms.HiddenInput)
    bank_statement = forms.FileField(
        required=False,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
        help_text="Optional. Uploaded files are stored privately.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, _metric, label, kind in MONTHLY_CHECKIN_FIELDS:
            if kind == "decimal":
                field = forms.DecimalField(
                    label=label,
                    required=False,
                    min_value=Decimal("0"),
                    max_digits=16,
                    decimal_places=2,
                    widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
                )
            elif kind == "integer":
                field = forms.IntegerField(
                    label=label,
                    required=False,
                    min_value=1 if name == "confidence_score" else 0,
                    max_value=10 if name == "confidence_score" else None,
                    widget=forms.NumberInput(attrs={"class": "form-control", "min": "1" if name == "confidence_score" else "0"}),
                )
            else:
                field = forms.CharField(
                    label=label,
                    required=False,
                    widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
                )
            self.fields[name] = field
            self.fields[f"{name}_not_applicable"] = forms.BooleanField(
                required=False,
                label="Not applicable",
                widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            )

    def clean_bank_statement(self):
        uploaded = self.cleaned_data.get("bank_statement")
        if uploaded:
            self.cleaned_data["bank_statement_content_type"] = validate_document_file(uploaded)
        return uploaded

    def clean(self):
        cleaned_data = super().clean()
        for name, _metric, _label, _kind in MONTHLY_CHECKIN_FIELDS:
            if cleaned_data.get(f"{name}_not_applicable") and cleaned_data.get(name) not in (None, ""):
                self.add_error(name, "Leave this field empty when marking it not applicable.")
        return cleaned_data

    def observations(self):
        for name, metric, _label, kind in MONTHLY_CHECKIN_FIELDS:
            value = self.cleaned_data.get(name)
            not_applicable = self.cleaned_data.get(f"{name}_not_applicable", False)
            if value in (None, "") and not not_applicable:
                continue
            values = {
                "availability": (
                    IncubateeMetricObservation.Availability.NOT_APPLICABLE
                    if not_applicable
                    else IncubateeMetricObservation.Availability.RECORDED
                ),
                "decimal_value": value if kind == "decimal" and not not_applicable else None,
                "integer_value": value if kind == "integer" and not not_applicable else None,
                "text_value": value if kind == "text" and not not_applicable else None,
            }
            yield metric, values


class QuarterlyReviewForm(forms.Form):
    period_start = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )
    tax_clearance_status = forms.CharField(
        required=False, max_length=32, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    cipc_annual_return_status = forms.CharField(
        required=False, max_length=32, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    management_accounts = forms.FileField(
        required=False, widget=forms.ClearableFileInput(attrs={"class": "form-control"})
    )
    finance_score = forms.ChoiceField(
        required=False,
        choices=[("", "Not recorded"), *[(str(value), str(value)) for value in range(1, 6)]],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    marketing_score = forms.ChoiceField(
        required=False,
        choices=[("", "Not recorded"), *[(str(value), str(value)) for value in range(1, 6)]],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    operations_score = forms.ChoiceField(
        required=False,
        choices=[("", "Not recorded"), *[(str(value), str(value)) for value in range(1, 6)]],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    compliance_score = forms.ChoiceField(
        required=False,
        choices=[("", "Not recorded"), *[(str(value), str(value)) for value in range(1, 6)]],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    technology_score = forms.ChoiceField(
        required=False,
        choices=[("", "Not recorded"), *[(str(value), str(value)) for value in range(1, 6)]],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    leadership_score = forms.ChoiceField(
        required=False,
        choices=[("", "Not recorded"), *[(str(value), str(value)) for value in range(1, 6)]],
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_period_start(self):
        period = self.cleaned_data["period_start"]
        if period.day != 1 or period.month not in (1, 4, 7, 10):
            raise ValidationError("Select the first day of a calendar quarter.")
        return period

    def clean_management_accounts(self):
        uploaded = self.cleaned_data.get("management_accounts")
        if uploaded:
            self.cleaned_data["management_accounts_content_type"] = validate_document_file(uploaded)
        return uploaded

    @staticmethod
    def safe_filename(uploaded_file):
        return Path(uploaded_file.name).name[:255]


class IncubateeMilestoneForm(forms.ModelForm):
    confirm_duplicate = forms.BooleanField(
        required=False,
        label="Confirm this is a separate milestone",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = IncubateeMilestone
        fields = ("milestone_type", "milestone_date", "value_zar", "description", "evidence")
        widgets = {
            "milestone_type": forms.Select(attrs={"class": "form-select"}),
            "milestone_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "value_zar": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "evidence": forms.ClearableFileInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, incubatee, **kwargs):
        super().__init__(*args, **kwargs)
        self.incubatee = incubatee

    def clean_value_zar(self):
        value = self.cleaned_data.get("value_zar")
        if value is not None and value < 0:
            raise ValidationError("Value cannot be negative.")
        return value

    def clean_evidence(self):
        uploaded = self.cleaned_data.get("evidence")
        if uploaded:
            self.cleaned_data["evidence_content_type"] = validate_document_file(uploaded)
        return uploaded

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("milestone_type") and cleaned_data.get("milestone_date"):
            duplicate = IncubateeMilestone.objects.filter(
                incubatee=self.incubatee,
                milestone_type=cleaned_data["milestone_type"],
                milestone_date=cleaned_data["milestone_date"],
                value_zar=cleaned_data.get("value_zar"),
            ).exists()
            if duplicate and not cleaned_data.get("confirm_duplicate"):
                self.add_error(
                    "confirm_duplicate",
                    "A milestone with the same type, date, and value exists. Confirm to record another.",
                )
        return cleaned_data

    @staticmethod
    def safe_filename(uploaded_file):
        return Path(uploaded_file.name).name[:255]


class MentoringSessionForm(forms.ModelForm):
    class Meta:
        model = MentoringSession
        fields = ("mentor", "session_date", "mentoring_hours", "topics", "progress_rating")
        widgets = {
            "mentor": forms.Select(attrs={"class": "form-select"}),
            "session_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "mentoring_hours": forms.NumberInput(attrs={"class": "form-control", "min": "0.01", "step": "0.01"}),
            "topics": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "progress_rating": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["mentor"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            Stakeholder.objects.filter(is_deleted=False),
        ).order_by("name")
        self.fields["mentor"].required = False
        self.fields["mentoring_hours"].min_value = Decimal("0.01")
        self.fields["progress_rating"].min_value = Decimal("0")


class IncubateeExitForm(forms.ModelForm):
    class Meta:
        model = IncubateeExit
        fields = ("exit_date", "outcome", "reason", "outcomes")
        widgets = {
            "exit_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "outcome": forms.Select(attrs={"class": "form-select"}),
            "reason": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "outcomes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class IncubateeFollowUpForm(forms.ModelForm):
    class Meta:
        model = IncubateeFollowUp
        fields = (
            "business_status",
            "revenue_zar",
            "full_time_employees",
            "part_time_employees",
            "funding_received_zar",
            "notes",
        )
        widgets = {
            "business_status": forms.Select(attrs={"class": "form-select"}),
            "revenue_zar": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
            "full_time_employees": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "part_time_employees": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "funding_received_zar": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
