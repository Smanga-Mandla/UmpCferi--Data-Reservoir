import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from accounts.models import AccessControlledModel, RoleGrant


def document_upload_path(instance, filename):
    extension = Path(filename).suffix.lower()
    if extension not in DocumentVersion.ALLOWED_EXTENSIONS:
        raise ValidationError("Unsupported document file extension.")
    return f"documents/{instance.document_id}/{instance.id}{extension}"


class Document(AccessControlledModel):
    access_resource = None

    class Category(models.TextChoices):
        INCORPORATION = "incorporation", "Incorporation (CIPC)"
        POLICIES = "policies", "Policies"
        QMS = "qms", "QMS"
        COIDA = "coida", "COIDA"
        SARS_TAX_PINS = "sars_tax_pins", "SARS and tax PINs"
        BBBEE = "bbbee", "B-BBEE"
        BANKING = "banking", "Banking"
        GOVERNANCE = "governance", "Governance and board minutes"
        CONTRACTS = "contracts", "Contracts"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=32, choices=Category.choices)
    reference_number = models.CharField(max_length=128, blank=True)
    issuing_authority = models.CharField(max_length=255, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="archived_centre_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_centre_documents",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("title", "id")

    @property
    def permission_resource(self):
        if self.sensitivity == self.Sensitivity.CONFIDENTIAL:
            return RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS
        return RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS

    @property
    def current_version(self):
        return self.versions.filter(is_current=True).first()

    @property
    def expiry_status(self):
        if self.expiry_date is None:
            return "no_expiry"
        today = timezone.localdate()
        if self.expiry_date < today:
            return "expired"
        if self.expiry_date <= today + timedelta(days=60):
            return "expiring"
        return "valid"

    def __str__(self):
        return self.title


class DocumentVersion(models.Model):
    ALLOWED_EXTENSIONS = frozenset(
        {".pdf", ".docx", ".xlsx", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff"}
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="versions")
    file = models.FileField(upload_to=document_upload_path, max_length=512)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=127)
    size_bytes = models.PositiveBigIntegerField()
    version_number = models.PositiveIntegerField()
    change_note = models.CharField(max_length=500, blank=True)
    metadata_snapshot = models.JSONField(default=dict)
    is_current = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_centre_document_versions",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-version_number",)
        constraints = [
            models.UniqueConstraint(
                fields=("document", "version_number"),
                name="unique_centre_document_version_number",
            ),
            models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(is_current=True),
                name="unique_current_centre_document_version",
            ),
        ]

    @property
    def sensitivity(self):
        return self.document.sensitivity

    @property
    def owner_id(self):
        return self.document.owner_id

    @property
    def access_resource(self):
        return self.document.permission_resource

    def __str__(self):
        return f"{self.document.title} v{self.version_number}"


class ExpiryAlert(models.Model):
    DAYS_BEFORE_CHOICES = ((60, "60 days"), (30, "30 days"), (7, "7 days"))

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="expiry_alerts")
    expiry_date = models.DateField()
    days_before = models.PositiveSmallIntegerField(choices=DAYS_BEFORE_CHOICES)
    due_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_error = models.TextField(blank=True)

    class Meta:
        ordering = ("due_date", "document__title")
        constraints = [
            models.UniqueConstraint(
                fields=("document", "expiry_date", "days_before"),
                name="unique_document_expiry_alert",
            ),
        ]

    def __str__(self):
        return f"{self.document.title}: expires in {self.days_before} days"
