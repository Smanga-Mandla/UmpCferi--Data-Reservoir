import re

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.access import accessible_queryset, has_access
from accounts.models import RoleGrant
from incubation.models import Incubatee
from stakeholders.models import (
    BoardMemberProfile,
    POPIAConsent,
    StaffProfile,
    StaffIncubateeAssignment,
    Stakeholder,
    StakeholderTypeAssignment,
)


def _phone_key(value):
    return "".join(re.findall(r"\d", value or ""))


class StakeholderForm(forms.ModelForm):
    relationship_types = forms.MultipleChoiceField(
        choices=Stakeholder.RelationshipType.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Stakeholder relationships",
    )

    class Meta:
        model = Stakeholder
        fields = ("name", "title", "email", "phone", "sensitivity")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.duplicate_matches = []
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxSelectMultiple):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = "form-control"
            else:
                field.widget.attrs["class"] = "form-control"
        if self.instance.pk:
            self.fields["relationship_types"].initial = list(
                self.instance.type_assignments.filter(is_active=True).values_list(
                    "relationship_type", flat=True
                )
            )
            if self.instance.partner_representative_links.filter(is_active=True).exists():
                current = self.fields["relationship_types"].initial
                representative_type = Stakeholder.RelationshipType.PARTNER_REPRESENTATIVE
                if representative_type not in current:
                    current.append(representative_type)

    def _visible_matches(self, email="", phone=""):
        matches = accessible_queryset(
            self.user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            Stakeholder.objects.exclude(pk=self.instance.pk or None),
        )
        query = Q()
        if email:
            query |= Q(email__iexact=email)
        phone_key = _phone_key(phone)
        if phone_key:
            for stakeholder in matches.only("pk", "phone"):
                if _phone_key(stakeholder.phone) == phone_key:
                    self.duplicate_matches.append(
                        (stakeholder, "phone")
                    )
        if query:
            for stakeholder in matches.filter(query):
                if not any(candidate.pk == stakeholder.pk for candidate, _ in self.duplicate_matches):
                    self.duplicate_matches.append((stakeholder, "email"))
        return matches

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            return None
        matches = self._visible_matches(email=email)
        if matches.filter(email__iexact=email).exists():
            raise ValidationError(
                "A stakeholder with this email already exists. Reuse the existing record."
            )
        return email

    def clean(self):
        cleaned_data = super().clean()
        phone = cleaned_data.get("phone", "")
        if phone:
            self._visible_matches(phone=phone)
        if (
            self.instance.pk
            and self.instance.partner_representative_links.filter(is_active=True).exists()
            and Stakeholder.RelationshipType.PARTNER_REPRESENTATIVE
            not in cleaned_data.get("relationship_types", ())
        ):
            self.add_error(
                "relationship_types",
                "Unlink active partner representative relationships before removing this type.",
            )
        return cleaned_data

    @transaction.atomic
    def save_relationships(self, stakeholder):
        if not self.is_valid():
            raise ValueError("Stakeholder relationship assignments require a valid form.")
        selected = set(self.cleaned_data.get("relationship_types", ()))
        assignments = stakeholder.type_assignments.filter(is_active=True)
        for assignment in assignments:
            if assignment.relationship_type not in selected:
                assignment.is_active = False
                assignment.ended_by = self.user
                assignment.ended_at = timezone.now()
                assignment.save(update_fields=("is_active", "ended_by", "ended_at"))
        existing = set(
            stakeholder.type_assignments.filter(is_active=True).values_list(
                "relationship_type", flat=True
            )
        )
        for relationship_type in selected - existing:
            StakeholderTypeAssignment.objects.create(
                stakeholder=stakeholder,
                relationship_type=relationship_type,
                assigned_by=self.user,
            )


class StakeholderDetailsForm(forms.Form):
    appointment_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )
    term_end = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )
    committees = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"})
    )
    declarations_of_interest = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 4, "class": "form-control"})
    )
    position = forms.CharField(
        required=False, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    contract_start = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )
    contract_end = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )
    skills = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"})
    )
    assigned_incubatees = forms.ModelMultipleChoiceField(
        queryset=Incubatee.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": 8}),
        label="Assigned incubatees",
    )

    def __init__(self, *args, stakeholder=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.stakeholder = stakeholder
        self.user = user
        candidates = accessible_queryset(
            user,
            RoleGrant.Resource.INCUBATEES,
            RoleGrant.Action.UPDATE,
            Incubatee.objects.all(),
        )
        allowed_ids = [
            incubatee.pk
            for incubatee in candidates
            if has_access(
                user,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.UPDATE,
                incubatee,
            )
        ]
        self.fields["assigned_incubatees"].queryset = (
            Incubatee.objects.filter(pk__in=allowed_ids).order_by("legal_name")
        )
        if stakeholder is None:
            return
        board = getattr(stakeholder, "board_profile", None)
        staff = getattr(stakeholder, "staff_profile", None)
        if board:
            self.initial.update(
                appointment_date=board.appointment_date,
                term_end=board.term_end,
                committees=board.committees,
                declarations_of_interest=board.declarations_of_interest,
            )
        if staff:
            self.initial.update(
                position=staff.position,
                contract_start=staff.contract_start,
                contract_end=staff.contract_end,
                skills=staff.skills,
                assigned_incubatees=staff.incubatee_assignments.filter(
                    is_active=True,
                    incubatee_id__in=allowed_ids,
                ).values_list("incubatee_id", flat=True),
            )

    def clean_assigned_incubatees(self):
        incubatees = self.cleaned_data["assigned_incubatees"]
        for incubatee in incubatees:
            if not has_access(
                self.user,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.UPDATE,
                incubatee,
            ):
                raise ValidationError(
                    "You do not have permission to assign this incubatee."
                )
        return incubatees

    @transaction.atomic
    def save_profiles(self, stakeholder):
        if not self.is_valid():
            raise ValueError("Stakeholder profiles require a valid form.")
        types = set(
            stakeholder.type_assignments.filter(is_active=True).values_list(
                "relationship_type", flat=True
            )
        )
        data = self.cleaned_data
        if Stakeholder.RelationshipType.BOARD_MEMBER in types:
            BoardMemberProfile.objects.update_or_create(
                stakeholder=stakeholder,
                defaults={
                    "appointment_date": data["appointment_date"],
                    "term_end": data["term_end"],
                    "committees": data["committees"],
                    "declarations_of_interest": data["declarations_of_interest"],
                },
            )
        if types & {
            Stakeholder.RelationshipType.STAFF,
            Stakeholder.RelationshipType.FREELANCER,
        }:
            staff_profile, _created = StaffProfile.objects.update_or_create(
                stakeholder=stakeholder,
                defaults={
                    "position": data["position"],
                    "contract_start": data["contract_start"],
                    "contract_end": data["contract_end"],
                    "skills": data["skills"],
                },
            )
            selected_ids = {
                incubatee.pk for incubatee in data["assigned_incubatees"]
            }
            manageable_assignments = staff_profile.incubatee_assignments.filter(
                is_active=True,
                incubatee_id__in=self.fields["assigned_incubatees"].queryset.values("pk"),
            )
            for assignment in manageable_assignments.exclude(
                incubatee_id__in=selected_ids
            ):
                assignment.is_active = False
                assignment.ended_by = self.user
                assignment.ended_at = timezone.now()
                assignment.save(
                    update_fields=("is_active", "ended_by", "ended_at")
                )
            existing_ids = set(
                manageable_assignments.filter(is_active=True).values_list(
                    "incubatee_id", flat=True
                )
            )
            for incubatee_id in selected_ids - existing_ids:
                StaffIncubateeAssignment.objects.create(
                    staff_profile=staff_profile,
                    incubatee_id=incubatee_id,
                    assigned_by=self.user,
                )
        elif self.stakeholder is not None:
            staff_profile = getattr(stakeholder, "staff_profile", None)
            if staff_profile:
                for assignment in staff_profile.incubatee_assignments.filter(
                    is_active=True
                ):
                    assignment.is_active = False
                    assignment.ended_by = self.user
                    assignment.ended_at = timezone.now()
                    assignment.save(
                        update_fields=("is_active", "ended_by", "ended_at")
                    )


class POPIAConsentForm(forms.ModelForm):
    class Meta:
        model = POPIAConsent
        fields = ("consent_date", "purpose", "method")
        widgets = {
            "consent_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "purpose": forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
            "method": forms.TextInput(attrs={"class": "form-control"}),
        }
