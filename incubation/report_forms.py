from django import forms

from incubation.account_access import manageable_businesses
from incubation.models import Cohort, Incubatee, IncubateeAccount, Programme


class StaffIncubateeMonitoringFilter(forms.Form):
    business = forms.ModelChoiceField(
        queryset=Incubatee.objects.none(), required=False, empty_label="All businesses"
    )
    programme = forms.ModelChoiceField(
        queryset=Programme.objects.none(), required=False, empty_label="All programmes"
    )
    cohort = forms.ModelChoiceField(
        queryset=Cohort.objects.none(), required=False, empty_label="All cohorts"
    )
    status = forms.ChoiceField(
        choices=(("", "All statuses"), *Incubatee.Status.choices), required=False
    )
    sector = forms.ChoiceField(required=False, label="Business sector")
    province = forms.ChoiceField(required=False)
    business_stage = forms.ChoiceField(
        choices=(("", "All stages"), *Incubatee.BusinessStage.choices),
        required=False,
        label="Business stage",
    )
    entrepreneur_category = forms.ChoiceField(
        choices=(("", "All categories"), *IncubateeAccount.EntrepreneurCategory.choices),
        required=False,
        label="Incubatee category",
    )
    reporting_period = forms.DateField(
        required=False,
        input_formats=("%Y-%m",),
        widget=forms.DateInput(attrs={"type": "month"}),
        label="Reporting period",
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        businesses = manageable_businesses(user)
        business_ids = businesses.values("pk")
        self.fields["business"].queryset = businesses.order_by("legal_name")
        self.fields["programme"].queryset = Programme.objects.filter(
            incubatees__in=business_ids
        ).distinct().order_by("name")
        self.fields["cohort"].queryset = Cohort.objects.filter(
            incubatees__in=business_ids
        ).distinct().order_by("name")
        sectors = (
            businesses.exclude(sub_sector="")
            .values_list("sub_sector", flat=True)
            .distinct()
            .order_by("sub_sector")
        )
        provinces = (
            businesses.exclude(province="")
            .values_list("province", flat=True)
            .distinct()
            .order_by("province")
        )
        self.fields["sector"].choices = [
            ("", "All sectors"),
            *((value, value) for value in sectors),
        ]
        self.fields["province"].choices = [
            ("", "All provinces"),
            *((value, value) for value in provinces),
        ]
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )

    def clean_reporting_period(self):
        period = self.cleaned_data.get("reporting_period")
        if period:
            return period.replace(day=1)
        return period
