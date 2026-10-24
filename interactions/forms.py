from django import forms
from django.contrib.auth import get_user_model

from accounts.access import accessible_queryset, has_access
from accounts.models import Role, RoleGrant
from incubation.models import Incubatee
from partners.models import Partner
from stakeholders.models import Stakeholder

from interactions.models import (
    EventAttendance,
    FollowUpTask,
    Interaction,
)

User = get_user_model()


class InteractionForm(forms.ModelForm):
    participants = forms.ModelMultipleChoiceField(
        queryset=Stakeholder.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": 6}),
    )
    additional_participants = forms.CharField(
        required=False,
        label="Other participants",
        help_text="One name per line for participants not yet in the Stakeholder Directory.",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    class Meta:
        model = Interaction
        fields = (
            "occurred_at",
            "duration_minutes",
            "interaction_type",
            "incubatees",
            "partners",
            "topics",
            "outcome",
            "sensitivity",
        )
        widgets = {
            "occurred_at": forms.DateTimeInput(
                attrs={"class": "form-control", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "duration_minutes": forms.NumberInput(
                attrs={"class": "form-control", "min": "0", "step": "1"}
            ),
            "interaction_type": forms.Select(attrs={"class": "form-select"}),
            "incubatees": forms.SelectMultiple(
                attrs={"class": "form-select", "size": 6}
            ),
            "partners": forms.SelectMultiple(
                attrs={"class": "form-select", "size": 6}
            ),
            "topics": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "outcome": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "sensitivity": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["occurred_at"].input_formats = ("%Y-%m-%dT%H:%M",)
        self.fields["incubatees"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.INCUBATEES,
            RoleGrant.Action.READ,
            Incubatee.objects.all(),
        )
        self.fields["partners"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.READ,
            Partner.objects.all(),
        )
        self.fields["participants"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            Stakeholder.objects.all(),
        )

    def clean_additional_participants(self):
        names = []
        seen = set()
        for raw_name in self.cleaned_data["additional_participants"].splitlines():
            name = raw_name.strip()
            normalized = name.casefold()
            if name and normalized not in seen:
                names.append(name[:255])
                seen.add(normalized)
        return names

    def clean_sensitivity(self):
        sensitivity = self.cleaned_data["sensitivity"]
        if (
            sensitivity == Interaction.Sensitivity.RESTRICTED
            and not self.user.is_superuser
            and not self.user.roles.filter(code=Role.Code.SUPER_ADMIN).exists()
        ):
            raise forms.ValidationError(
                "Restricted interactions can only be recorded by a Super Admin."
            )
        return sensitivity

    def clean(self):
        cleaned_data = super().clean()
        for field_name, resource in (
            ("incubatees", RoleGrant.Resource.INCUBATEES),
            ("partners", RoleGrant.Resource.PARTNERS_MOUS),
            ("participants", RoleGrant.Resource.STAKEHOLDERS),
        ):
            for record in cleaned_data.get(field_name, ()):
                if not has_access(self.user, resource, RoleGrant.Action.READ, record):
                    self.add_error(field_name, "You cannot link a record you cannot access.")
                    break
        return cleaned_data


class InteractionFilterForm(forms.Form):
    interaction_type = forms.ChoiceField(
        required=False,
        choices=(("", "All types"), *Interaction.Type.choices),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    incubatee = forms.ModelChoiceField(
        queryset=Incubatee.objects.none(),
        required=False,
        empty_label="All incubatees",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    partner = forms.ModelChoiceField(
        queryset=Partner.objects.none(),
        required=False,
        empty_label="All partners",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["incubatee"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.INCUBATEES,
            RoleGrant.Action.READ,
            Incubatee.objects.all(),
        )
        self.fields["partner"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.READ,
            Partner.objects.all(),
        )

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("date_from")
        end = cleaned_data.get("date_to")
        if start and end and end < start:
            self.add_error("date_to", "End date cannot be before start date.")
        return cleaned_data


class FollowUpTaskForm(forms.ModelForm):
    class Meta:
        model = FollowUpTask
        fields = ("description", "owner", "due_date", "status")
        widgets = {
            "description": forms.TextInput(attrs={"class": "form-control"}),
            "owner": forms.Select(attrs={"class": "form-select"}),
            "due_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, user, interaction, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.interaction = interaction
        eligible_owners = [
            candidate.pk
            for candidate in User.objects.filter(is_active=True).order_by("email")
            if has_access(
                candidate,
                RoleGrant.Resource.INTERACTIONS,
                RoleGrant.Action.READ,
                interaction,
            )
        ]
        self.fields["owner"].queryset = User.objects.filter(
            pk__in=eligible_owners
        ).order_by("email")

    def clean_owner(self):
        owner = self.cleaned_data["owner"]
        if not has_access(
            owner,
            RoleGrant.Resource.INTERACTIONS,
            RoleGrant.Action.READ,
            self.interaction,
        ):
            raise forms.ValidationError(
                "The assigned owner must have permission to access this interaction."
            )
        return owner


class EventAttendanceForm(forms.ModelForm):
    class Meta:
        model = EventAttendance
        fields = ("stakeholder", "participant_name", "status")
        widgets = {
            "stakeholder": forms.Select(attrs={"class": "form-select"}),
            "participant_name": forms.TextInput(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, user, interaction, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stakeholder"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            interaction.participants.all(),
        )
        self.fields["stakeholder"].required = False
        self.fields["participant_name"].required = False

    def clean(self):
        cleaned_data = super().clean()
        stakeholder = cleaned_data.get("stakeholder")
        participant_name = (cleaned_data.get("participant_name") or "").strip()
        if bool(stakeholder) == bool(participant_name):
            raise forms.ValidationError(
                "Select a Stakeholder Directory record or enter a participant name, not both."
            )
        cleaned_data["participant_name"] = participant_name
        return cleaned_data
