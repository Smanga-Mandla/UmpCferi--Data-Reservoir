from datetime import date
from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from documents.validators import validate_document_file
from incubation.models import IncubateeBusinessChallenge, IncubateeMilestone, IncubateeProgressReview
from incubation.models import Incubatee


class IncubateeProgressForm(forms.Form):
    reporting_month = forms.ChoiceField(choices=[(month, date(2000, month, 1).strftime("%B")) for month in range(1, 13)])
    reporting_year = forms.IntegerField(min_value=1900, max_value=date.today().year)
    revenue_zar = forms.DecimalField(
        min_value=0, max_digits=16, decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        help_text="Monthly revenue. Enter 0 if known to be zero; leave blank if unknown.",
    )
    customer_count = forms.IntegerField(
        min_value=0,
        required=False,
        help_text="Reported customer count. Enter 0 if known to be zero; leave blank if unknown.",
    )
    full_time_employees = forms.IntegerField(
        min_value=0,
        required=False,
        help_text="Enter 0 if known to have no full-time employees; leave blank if unknown.",
    )
    part_time_employees = forms.IntegerField(
        min_value=0,
        required=False,
        help_text="Enter 0 if known to have no part-time employees; leave blank if unknown.",
    )
    full_time_men = forms.IntegerField(min_value=0, required=False)
    full_time_women = forms.IntegerField(min_value=0, required=False)
    full_time_youth = forms.IntegerField(min_value=0, required=False)
    part_time_men = forms.IntegerField(min_value=0, required=False)
    part_time_women = forms.IntegerField(min_value=0, required=False)
    part_time_youth = forms.IntegerField(min_value=0, required=False)
    funding_received_zar = forms.DecimalField(
        min_value=0, max_digits=16, decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        help_text="Funding received during this reporting period, not cumulative funding. Enter 0 if none; leave blank if unknown.",
    )
    funding_source = forms.CharField(
        required=False,
        max_length=1000,
        widget=forms.TextInput(),
        help_text="Source(s) of funding received this month, if applicable.",
    )
    business_stage = forms.ChoiceField(
        choices=(("", "Not provided"), *Incubatee.BusinessStage.choices),
        required=False,
    )
    support_needed = forms.CharField(
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Support needed",
    )
    business_achievements = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    challenges_experienced = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    additional_comments = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reporting_year"].max_value = timezone.localdate().year
        self.fields["reporting_month"].choices = [
            (month, date(2000, month, 1).strftime("%B")) for month in range(1, 13)
        ]
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )

    def clean_reporting_year(self):
        year = self.cleaned_data["reporting_year"]
        if year > timezone.localdate().year:
            raise ValidationError("Reporting year cannot be in the future.")
        return year

    def clean(self):
        cleaned = super().clean()
        pairs = (
            ("full_time_men", "full_time_women", "full_time_employees"),
            ("part_time_men", "part_time_women", "part_time_employees"),
        )
        for men, women, total in pairs:
            if all(cleaned.get(name) is not None for name in (men, women, total)):
                if cleaned[men] + cleaned[women] > cleaned[total]:
                    self.add_error(men, "Men and women cannot exceed the total in this employment group.")
        for youth, total in (
            ("full_time_youth", "full_time_employees"),
            ("part_time_youth", "part_time_employees"),
        ):
            if (
                cleaned.get(youth) is not None
                and cleaned.get(total) is not None
                and cleaned[youth] > cleaned[total]
            ):
                self.add_error(youth, "Youth count cannot exceed the employment total.")
        if "reporting_month" in cleaned and "reporting_year" in cleaned:
            month = int(cleaned["reporting_month"])
            period = date(cleaned["reporting_year"], month, 1)
            if period > timezone.localdate().replace(day=1):
                self.add_error("reporting_month", "Reporting period cannot be in the future.")
            cleaned["period_start"] = period
        return cleaned

    def metric_values(self):
        return {
            name: self.cleaned_data[name]
            for name in (
                "revenue_zar",
                "customer_count",
                "full_time_employees",
                "part_time_employees",
                "full_time_men",
                "full_time_women",
                "full_time_youth",
                "part_time_men",
                "part_time_women",
                "part_time_youth",
                "funding_received_zar",
                "funding_source",
                "business_stage",
                "support_needed",
            )
        }


class IncubateeMilestoneForm(forms.Form):
    title = forms.CharField(max_length=255)
    milestone_type = forms.ChoiceField(choices=IncubateeMilestone.Type.choices)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    achieved_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    evidence = forms.FileField(required=False)

    def clean_evidence(self):
        file = self.cleaned_data.get("evidence")
        if file:
            self.cleaned_data["detected_content_type"] = validate_document_file(file)
        return file

    @staticmethod
    def safe_filename(file):
        return Path(file.name.replace("\\", "/")).name[:255]


class IncubateeChallengeForm(forms.Form):
    category = forms.ChoiceField(choices=IncubateeBusinessChallenge.Category.choices)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))
    evidence = forms.FileField(required=False)

    def clean_evidence(self):
        file = self.cleaned_data.get("evidence")
        if file:
            self.cleaned_data["detected_content_type"] = validate_document_file(file)
        return file

    @staticmethod
    def safe_filename(file):
        return Path(file.name.replace("\\", "/")).name[:255]


class IncubateeReviewForm(forms.Form):
    status = forms.ChoiceField(choices=IncubateeProgressReview.Status.choices)
    review_note = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        if choices is not None:
            self.fields["status"].choices = choices
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )
