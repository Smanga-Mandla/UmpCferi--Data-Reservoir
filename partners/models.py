import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import models, transaction

from accounts.models import AccessControlledModel, RoleGrant
from stakeholders.models import Stakeholder


def create_mou_folder(mou_id):
    storage = FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT)
    folder = Path(storage.path(f"mous/{mou_id}"))
    folder.mkdir(parents=True, exist_ok=True)


def mou_attachment_path(instance, filename):
    extension = Path(filename).suffix.lower()[:16]
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in extension):
        extension = ""
    return f"mous/{instance.mou_id}/{instance.category}/{instance.id}{extension}"


def partner_image_path(instance, filename):
    extension = Path(filename).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        extension = ".img"
    return f"partners/{instance.partner_id}/images/{instance.id}{extension}"


class Partner(AccessControlledModel):
    access_resource = RoleGrant.Resource.PARTNERS_MOUS

    class OrganisationType(models.TextChoices):
        GOVERNMENT = "government", "Government"
        PRIVATE = "private", "Private"
        NGO = "ngo", "NGO"
        ACADEMIC = "academic", "Academic"
        FUNDER = "funder", "Funder"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    registration_number = models.CharField(max_length=128, blank=True)
    organisation_type = models.CharField(max_length=16, choices=OrganisationType.choices)
    sector = models.CharField(max_length=128, blank=True)
    address = models.TextField(blank=True)
    website = models.URLField(blank=True)
    province = models.CharField(max_length=128, blank=True)
    bbbee_level = models.CharField(max_length=64, blank=True, verbose_name="B-BBEE level")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_partner_organisations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    representatives = models.ManyToManyField(
        Stakeholder,
        through="PartnerRepresentative",
        related_name="partner_organisations",
        blank=True,
    )

    class Meta:
        ordering = ("name", "id")

    def __str__(self):
        return self.name

    @property
    def current_image(self):
        return self.images.first()


class PartnerImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.ForeignKey(
        Partner,
        on_delete=models.PROTECT,
        related_name="images",
    )
    file = models.ImageField(upload_to=partner_image_path, max_length=512)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_partner_images",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-uploaded_at", "-pk")

    def __str__(self):
        return f"Image for {self.partner} uploaded {self.uploaded_at}"


class PartnerRepresentative(models.Model):
    partner = models.ForeignKey(Partner, on_delete=models.CASCADE, related_name="representative_links")
    stakeholder = models.ForeignKey(
        Stakeholder,
        on_delete=models.PROTECT,
        related_name="partner_representative_links",
    )
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="linked_partner_representatives",
    )
    linked_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    unlinked_at = models.DateTimeField(null=True, blank=True)
    unlinked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="unlinked_partner_representatives",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("partner", "stakeholder"),
                condition=models.Q(is_active=True),
                name="unique_partner_stakeholder_link",
            ),
        ]
        ordering = ("stakeholder__name",)

    def __str__(self):
        return f"{self.stakeholder} at {self.partner}"


class MOU(AccessControlledModel):
    access_resource = RoleGrant.Resource.PARTNERS_MOUS

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        TERMINATED = "terminated", "Terminated"
        RENEWED = "renewed", "Renewed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.ForeignKey(Partner, on_delete=models.PROTECT, related_name="mous")
    title = models.CharField(max_length=255)
    reference = models.CharField(max_length=128, blank=True)
    purpose = models.TextField(blank=True)
    signing_date = models.DateField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True, db_index=True)
    renewal_terms = models.TextField(blank=True)
    value_zar = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Value (ZAR)",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    cferi_signatory = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="CFERI signatory",
    )
    primary_representative = models.ForeignKey(
        Stakeholder,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="primary_mous",
    )
    secondary_representative = models.ForeignKey(
        Stakeholder,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="secondary_mous",
    )
    renewed_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="renewals",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_mous",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "title")

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            transaction.on_commit(lambda: create_mou_folder(self.pk))

    def snapshot(self):
        return {
            "partner_id": str(self.partner_id),
            "sensitivity": self.sensitivity,
            "title": self.title,
            "reference": self.reference,
            "purpose": self.purpose,
            "signing_date": self.signing_date.isoformat() if self.signing_date else None,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "renewal_terms": self.renewal_terms,
            "value_zar": str(self.value_zar) if self.value_zar is not None else None,
            "status": self.status,
            "cferi_signatory": self.cferi_signatory,
            "primary_representative_id": (
                str(self.primary_representative_id) if self.primary_representative_id else None
            ),
            "secondary_representative_id": (
                str(self.secondary_representative_id)
                if self.secondary_representative_id
                else None
            ),
            "renewed_from_id": str(self.renewed_from_id) if self.renewed_from_id else None,
        }

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "The end date cannot be before the start date."})
        if self.value_zar is not None and self.value_zar < 0:
            raise ValidationError({"value_zar": "The agreement value cannot be negative."})
        representative_ids = {
            self.primary_representative_id,
            self.secondary_representative_id,
        } - {None}
        linked_representatives = PartnerRepresentative.objects.filter(
            partner_id=self.partner_id,
            stakeholder_id__in=representative_ids,
            is_active=True,
        ).values("stakeholder_id").distinct().count()
        if representative_ids and linked_representatives != len(representative_ids):
            raise ValidationError("MOU representatives must be linked to the selected partner.")

    def __str__(self):
        return self.title


class ImmutableMOUHistoryQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionDenied("MOU history entries are immutable.")

    def delete(self):
        raise PermissionDenied("MOU history entries are immutable.")


class MOUHistory(models.Model):
    mou = models.ForeignKey(MOU, on_delete=models.PROTECT, related_name="history")
    snapshot = models.JSONField()
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="mou_history_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    objects = ImmutableMOUHistoryQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise PermissionDenied("MOU history entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("MOU history entries are immutable.")

    def __str__(self):
        return f"{self.mou.title} history at {self.created_at}"


class MOUAttachment(models.Model):
    class Category(models.TextChoices):
        SIGNED_MOU = "signed_mou", "Signed MOU"
        ANNEXURE = "annexure", "Annexure"
        CORRESPONDENCE = "correspondence", "Correspondence"
        REPORT = "report", "Report"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mou = models.ForeignKey(MOU, on_delete=models.PROTECT, related_name="attachments")
    category = models.CharField(max_length=24, choices=Category.choices)
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to=mou_attachment_path, max_length=512)
    original_filename = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_mou_attachments",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("category", "-uploaded_at")

    def __str__(self):
        return self.title


class MOUObligation(AccessControlledModel):
    access_resource = RoleGrant.Resource.PARTNERS_MOUS

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    mou = models.ForeignKey(MOU, on_delete=models.PROTECT, related_name="obligations")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_mou_obligations",
    )
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_mou_obligations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("due_date", "title")

    def __str__(self):
        return self.title


class MOUExpiryAlert(models.Model):
    DAYS_BEFORE_CHOICES = ((90, "90 days"), (60, "60 days"), (30, "30 days"))

    mou = models.ForeignKey(MOU, on_delete=models.PROTECT, related_name="expiry_alerts")
    end_date = models.DateField()
    days_before = models.PositiveSmallIntegerField(choices=DAYS_BEFORE_CHOICES)
    due_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_error = models.TextField(blank=True)

    class Meta:
        ordering = ("due_date", "mou__title")
        constraints = [
            models.UniqueConstraint(
                fields=("mou", "end_date", "days_before"),
                name="unique_mou_expiry_alert",
            ),
        ]

    def __str__(self):
        return f"{self.mou.title}: renewal reminder {self.days_before} days before expiry"
