from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from accounts.access import accessible_queryset
from accounts.models import RoleGrant
from partners.models import (
    MOU,
    MOUAttachment,
    MOUObligation,
    Partner,
    PartnerRepresentative,
)
from stakeholders.models import Stakeholder

User = get_user_model()


class BootstrapModelForm(forms.ModelForm):
    def _apply_bootstrap(self):
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = "form-control"
            else:
                field.widget.attrs["class"] = "form-control"


class PartnerForm(BootstrapModelForm):
    image = forms.ImageField(
        required=False,
        label="Partner image or logo",
        help_text="JPEG, PNG, or WebP; maximum 5 MB. Earlier uploads are retained.",
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
    )

    class Meta:
        model = Partner
        fields = (
            "name",
            "registration_number",
            "organisation_type",
            "sector",
            "address",
            "website",
            "province",
            "bbbee_level",
            "sensitivity",
        )
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if not image:
            return image
        if image.size > 5 * 1024 * 1024:
            raise ValidationError("Partner images must be 5 MB or smaller.")

        validated_image = getattr(image, "image", None)
        image_format = getattr(validated_image, "format", "")
        if image_format not in {"JPEG", "PNG", "WEBP"}:
            raise ValidationError("Upload a valid JPEG, PNG, or WebP image.")
        if max(validated_image.size) > 4096:
            raise ValidationError("Partner images cannot exceed 4096 pixels on either side.")
        return image


class PartnerRepresentativeForm(forms.Form):
    stakeholder = forms.ModelChoiceField(
        queryset=Stakeholder.objects.none(),
        label="Existing stakeholder",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, user, partner, *args, **kwargs):
        super().__init__(*args, **kwargs)
        linked_ids = PartnerRepresentative.objects.filter(
            partner=partner,
            is_active=True,
        ).values_list("stakeholder_id", flat=True)
        self.fields["stakeholder"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            Stakeholder.objects.exclude(pk__in=linked_ids),
        )


class MOUForm(BootstrapModelForm):
    class Meta:
        model = MOU
        fields = (
            "partner",
            "title",
            "reference",
            "purpose",
            "signing_date",
            "start_date",
            "end_date",
            "renewal_terms",
            "value_zar",
            "status",
            "cferi_signatory",
            "primary_representative",
            "secondary_representative",
            "renewed_from",
            "sensitivity",
        )
        widgets = {
            "purpose": forms.Textarea(attrs={"rows": 3}),
            "renewal_terms": forms.Textarea(attrs={"rows": 3}),
            "signing_date": forms.DateInput(attrs={"type": "date"}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "value_zar": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
        }

    def __init__(self, user, *args, instance=None, initial_partner=None, **kwargs):
        super().__init__(*args, instance=instance or MOU(), **kwargs)
        instance = self.instance
        self._apply_bootstrap()
        partners = accessible_queryset(
            user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.READ,
            Partner.objects.all(),
        )
        self.fields["partner"].queryset = partners
        readable_partner_ids = partners.values_list("pk", flat=True)
        self.fields["renewed_from"].queryset = accessible_queryset(
            user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.READ,
            MOU.objects.filter(partner_id__in=readable_partner_ids),
        ).exclude(pk=instance.pk if instance.pk else None)

        selected_partner_id = (
            self.data.get("partner")
            if self.is_bound
            else (instance.partner_id or getattr(initial_partner, "pk", None))
        )
        can_read_selected_partner = bool(
            selected_partner_id and partners.filter(pk=selected_partner_id).exists()
        )
        stakeholder_queryset = Stakeholder.objects.none()
        if can_read_selected_partner:
            stakeholder_queryset = Stakeholder.objects.filter(
                partner_representative_links__partner_id=selected_partner_id,
                partner_representative_links__is_active=True,
            ).distinct()
        stakeholder_queryset = accessible_queryset(
            user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            stakeholder_queryset,
        )
        self.fields["primary_representative"].queryset = stakeholder_queryset
        self.fields["secondary_representative"].queryset = stakeholder_queryset
        if initial_partner is not None and not self.is_bound:
            self.fields["partner"].initial = initial_partner

    def clean(self):
        cleaned_data = super().clean()
        partner = cleaned_data.get("partner")
        if not partner:
            return cleaned_data
        for field_name in ("primary_representative", "secondary_representative"):
            representative = cleaned_data.get(field_name)
            if representative and not PartnerRepresentative.objects.filter(
                partner=partner,
                stakeholder=representative,
            ).exists():
                self.add_error(field_name, "Choose a representative linked to this partner.")
        if (
            cleaned_data.get("primary_representative")
            and cleaned_data.get("primary_representative")
            == cleaned_data.get("secondary_representative")
        ):
            self.add_error("secondary_representative", "Choose a different secondary representative.")
        renewed_from = cleaned_data.get("renewed_from")
        if renewed_from and renewed_from.partner_id != partner.pk:
            self.add_error("renewed_from", "Link a renewal to an agreement for the same partner.")
        return cleaned_data


class MOUAttachmentForm(BootstrapModelForm):
    class Meta:
        model = MOUAttachment
        fields = ("category", "title", "file")
        widgets = {"file": forms.ClearableFileInput(attrs={"class": "form-control"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class MOUObligationForm(BootstrapModelForm):
    class Meta:
        model = MOUObligation
        fields = ("title", "description", "owner", "due_date", "status")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["owner"].queryset = User.objects.filter(is_active=True).order_by("email")
