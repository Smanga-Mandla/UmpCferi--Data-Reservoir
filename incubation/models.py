import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import AccessControlledModel, RoleGrant
from stakeholders.models import Stakeholder


def create_incubatee_folder(incubatee_id):
    storage = FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT)
    folder = Path(storage.path(f"incubatees/{incubatee_id}"))
    folder.mkdir(parents=True, exist_ok=True)


def incubatee_document_path(instance, filename):
    extension = Path(filename).suffix.lower()[:16]
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in extension):
        extension = ""
    return f"incubatees/{instance.incubatee_id}/{instance.pk}{extension}"


def business_document_path(instance, filename):
    extension = Path(filename).suffix.lower()[:16]
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in extension):
        extension = ""
    return (
        f"business-documents/{instance.document.incubatee_id}/"
        f"{instance.document_id}/{instance.pk}{extension}"
    )


def support_request_evidence_path(instance, filename):
    extension = Path(filename).suffix.lower()[:16]
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in extension):
        extension = ""
    return f"incubatees/{instance.incubatee_id}/support-requests/{uuid.uuid4()}{extension}"


def business_development_need_evidence_path(instance, filename):
    extension = Path(filename).suffix.lower()[:16]
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in extension):
        extension = ""
    return (
        f"incubatees/{instance.incubatee_id}/business-development/"
        f"{uuid.uuid4()}{extension}"
    )


def business_development_evidence_path(instance, filename):
    extension = Path(filename).suffix.lower()[:16]
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in extension):
        extension = ""
    return (
        f"incubatees/{instance.need.incubatee_id}/business-development/"
        f"{uuid.uuid4()}{extension}"
    )


class Programme(models.Model):
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Cohort(models.Model):
    programme = models.ForeignKey(
        Programme, on_delete=models.PROTECT, related_name="cohorts"
    )
    name = models.CharField(max_length=255)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("programme__name", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("programme", "name"),
                name="unique_cohort_name_per_programme",
            )
        ]

    def __str__(self):
        return f"{self.programme}: {self.name}"


class Incubatee(AccessControlledModel):
    access_resource = RoleGrant.Resource.INCUBATEES

    class Status(models.TextChoices):
        APPLICANT = "applicant", "Applicant"
        ONBOARDING = "onboarding", "Onboarding"
        ACTIVE = "active", "Active"
        GRADUATED = "graduated", "Graduated"
        EXITED = "exited", "Exited"
        WITHDRAWN = "withdrawn", "Withdrawn"

    class EntityType(models.TextChoices):
        PRIVATE_COMPANY = "private_company", "Private company"
        PUBLIC_COMPANY = "public_company", "Public company"
        NON_PROFIT = "non_profit", "Non-profit"
        COOPERATIVE = "cooperative", "Co-operative"
        SOLE_PROPRIETOR = "sole_proprietor", "Sole proprietor"
        PARTNERSHIP = "partnership", "Partnership"
        OTHER = "other", "Other"

    class VATStatus(models.TextChoices):
        REGISTERED = "registered", "Registered"
        NOT_REGISTERED = "not_registered", "Not registered"
        PENDING = "pending", "Pending"
        UNKNOWN = "unknown", "Unknown"

    class UrbanRural(models.TextChoices):
        URBAN = "urban", "Urban"
        RURAL = "rural", "Rural"

    class BusinessModel(models.TextChoices):
        B2B = "b2b", "B2B"
        B2C = "b2c", "B2C"
        B2G = "b2g", "B2G"

    class BusinessStage(models.TextChoices):
        IDEA = "idea", "Idea"
        MVP = "mvp", "MVP"
        EARLY_REVENUE = "early_revenue", "Early revenue"
        GROWTH = "growth", "Growth"

    class RegistrationStatus(models.TextChoices):
        UNKNOWN = "unknown", "Not specified"
        NOT_REGISTERED = "not_registered", "Not registered"
        PENDING = "pending", "Registration pending"
        REGISTERED = "registered", "Registered"

    class Formality(models.TextChoices):
        FORMAL = "formal", "Formal"
        INFORMAL = "informal", "Informal"

    class DisabilityStatus(models.TextChoices):
        YES = "yes", "Yes"
        NO = "no", "No"
        PREFER_NOT_TO_SAY = "prefer_not_to_say", "Prefer not to say"

    class ConsentStatus(models.TextChoices):
        GRANTED = "granted", "Granted"
        DECLINED = "declined", "Declined"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    legal_name = models.CharField(max_length=255)
    trading_name = models.CharField(max_length=255, blank=True)
    cipc_registration_number = models.CharField(max_length=128, blank=True, null=True, unique=True)
    date_registered = models.DateField(null=True, blank=True)
    business_registration_status = models.CharField(
        max_length=24,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.UNKNOWN,
    )
    entity_type = models.CharField(max_length=32, choices=EntityType.choices, blank=True)
    vat_status = models.CharField(max_length=24, choices=VATStatus.choices, blank=True)
    tax_number = models.CharField(max_length=128, blank=True)
    bbbee_level = models.CharField(max_length=64, blank=True)
    physical_address = models.TextField(blank=True)
    province = models.CharField(max_length=128, blank=True)
    municipality = models.CharField(max_length=128, blank=True)
    business_contact_email = models.EmailField(blank=True)
    business_contact_number = models.CharField(max_length=32, blank=True)
    urban_rural = models.CharField(max_length=16, choices=UrbanRural.choices, blank=True)

    sic_code = models.CharField(max_length=32, blank=True)
    sub_sector = models.CharField(max_length=255, blank=True)
    products_services = models.TextField(blank=True)
    business_model = models.CharField(max_length=8, choices=BusinessModel.choices, blank=True)
    business_stage = models.CharField(max_length=24, choices=BusinessStage.choices, blank=True)
    formality = models.CharField(max_length=16, choices=Formality.choices, blank=True)

    full_time_employees = models.PositiveIntegerField(null=True, blank=True)
    part_time_employees = models.PositiveIntegerField(null=True, blank=True)
    full_time_men = models.PositiveIntegerField(null=True, blank=True)
    full_time_women = models.PositiveIntegerField(null=True, blank=True)
    full_time_youth = models.PositiveIntegerField(null=True, blank=True)
    part_time_men = models.PositiveIntegerField(null=True, blank=True)
    part_time_women = models.PositiveIntegerField(null=True, blank=True)
    part_time_youth = models.PositiveIntegerField(null=True, blank=True)

    revenue_12_months_zar = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    monthly_average_revenue_zar = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    funding_received_zar = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    funding_sources = models.TextField(blank=True)
    debt_zar = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    has_bank_account = models.BooleanField(null=True, blank=True)

    customer_count = models.PositiveIntegerField(null=True, blank=True)
    top_customer_types = models.JSONField(default=list, blank=True)
    exports = models.BooleanField(null=True, blank=True)

    finance_score = models.PositiveSmallIntegerField(null=True, blank=True)
    marketing_score = models.PositiveSmallIntegerField(null=True, blank=True)
    operations_score = models.PositiveSmallIntegerField(null=True, blank=True)
    compliance_score = models.PositiveSmallIntegerField(null=True, blank=True)
    technology_score = models.PositiveSmallIntegerField(null=True, blank=True)
    leadership_score = models.PositiveSmallIntegerField(null=True, blank=True)
    top_support_needs = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.APPLICANT)
    programme = models.ForeignKey(
        Programme, null=True, blank=True, on_delete=models.PROTECT, related_name="incubatees"
    )
    cohort = models.ForeignKey(
        Cohort, null=True, blank=True, on_delete=models.PROTECT, related_name="incubatees"
    )
    staff_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owned_incubatees",
    )
    mentors = models.ManyToManyField(
        Stakeholder, blank=True, related_name="mentored_incubatees"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_incubatees"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="updated_incubatees",
    )
    updated_at = models.DateTimeField(auto_now=True)
    source = models.CharField(max_length=64, default="manual")

    class Meta:
        ordering = ("legal_name", "id")
        constraints = [
            models.CheckConstraint(
                condition=Q(finance_score__isnull=True) | Q(finance_score__gte=1, finance_score__lte=5),
                name="incubatee_finance_score_1_to_5",
            ),
            models.CheckConstraint(
                condition=Q(marketing_score__isnull=True) | Q(marketing_score__gte=1, marketing_score__lte=5),
                name="incubatee_marketing_score_1_to_5",
            ),
            models.CheckConstraint(
                condition=Q(operations_score__isnull=True) | Q(operations_score__gte=1, operations_score__lte=5),
                name="incubatee_operations_score_1_to_5",
            ),
            models.CheckConstraint(
                condition=Q(compliance_score__isnull=True) | Q(compliance_score__gte=1, compliance_score__lte=5),
                name="incubatee_compliance_score_1_to_5",
            ),
            models.CheckConstraint(
                condition=Q(technology_score__isnull=True) | Q(technology_score__gte=1, technology_score__lte=5),
                name="incubatee_technology_score_1_to_5",
            ),
            models.CheckConstraint(
                condition=Q(leadership_score__isnull=True) | Q(leadership_score__gte=1, leadership_score__lte=5),
                name="incubatee_leadership_score_1_to_5",
            ),
            models.CheckConstraint(
                condition=Q(revenue_12_months_zar__isnull=True) | Q(revenue_12_months_zar__gte=0),
                name="incubatee_revenue_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(funding_received_zar__isnull=True) | Q(funding_received_zar__gte=0),
                name="incubatee_funding_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(debt_zar__isnull=True) | Q(debt_zar__gte=0),
                name="incubatee_debt_nonnegative",
            ),
        ]

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            transaction.on_commit(lambda: create_incubatee_folder(self.pk))

    def clean(self):
        super().clean()
        if self.cohort_id and self.programme_id and self.cohort.programme_id != self.programme_id:
            raise ValidationError({"cohort": "Choose a cohort belonging to the selected programme."})

    def __str__(self):
        return self.trading_name or self.legal_name


class IncubateeAccount(models.Model):
    class EntrepreneurCategory(models.TextChoices):
        STUDENT_ENTREPRENEUR = "student_entrepreneur", "Student Entrepreneur"
        EXTERNAL_SME = "external_sme", "External SME"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_account",
    )
    entrepreneur_category = models.CharField(
        max_length=32,
        choices=EntrepreneurCategory.choices,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_incubatee_accounts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    invitation_sent_at = models.DateTimeField(null=True, blank=True)
    invitation_accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("user__last_name", "user__first_name", "user__email")

    @property
    def invitation_status(self):
        if self.invitation_accepted_at:
            return "accepted"
        if not self.invitation_sent_at:
            return "not_sent"
        if self.invitation_sent_at + timedelta(
            seconds=settings.PASSWORD_RESET_TIMEOUT
        ) <= timezone.now():
            return "expired"
        return "pending"

    @property
    def account_status(self):
        if not self.user.is_active:
            return "Suspended"
        if not self.invitation_accepted_at:
            return "Pending activation"
        return "Active"


class IncubateeAccountBusiness(models.Model):
    account = models.ForeignKey(
        IncubateeAccount,
        on_delete=models.PROTECT,
        related_name="business_links",
    )
    incubatee = models.ForeignKey(
        Incubatee,
        on_delete=models.PROTECT,
        related_name="account_links",
    )
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="linked_incubatee_accounts",
    )
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("incubatee__legal_name",)
        constraints = [
            models.UniqueConstraint(
                fields=("account", "incubatee"),
                name="unique_account_business_link",
            ),
        ]


class IncubateeProfileChange(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        STALE = "stale", "Needs resubmission"

    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="profile_change_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_profile_change_requests",
    )
    request_note = models.TextField(blank=True)
    old_values = models.JSONField()
    proposed_values = models.JSONField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_incubatee_profile_changes",
    )
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and any(
                getattr(previous, field) != getattr(self, field)
                for field in (
                    "incubatee_id",
                    "requested_by_id",
                    "request_note",
                    "old_values",
                    "proposed_values",
                    "created_at",
                )
            ):
                raise PermissionDenied("Profile change request details are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Profile change requests are retained as history.")


class BusinessDocument(models.Model):
    class Category(models.TextChoices):
        CIPC_REGISTRATION = "cipc_registration", "CIPC registration document"
        SARS_TAX = "sars_tax", "SARS tax document"
        BBBEE = "bbbee", "B-BBEE certificate or affidavit"
        BUSINESS_PLAN = "business_plan", "Business plan"
        FINANCIAL_STATEMENT = "financial_statement", "Financial statement"
        PROOF_OF_ADDRESS = "proof_of_address", "Proof of business address"
        OTHER = "other", "Other approved SME document"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="business_documents"
    )
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=32, choices=Category.choices)
    description = models.TextField(blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_business_documents",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_business_documents",
    )
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("category", "title", "created_at")

    @property
    def current_version(self):
        return self.versions.filter(is_current=True).first()

    @property
    def display_status(self):
        if self.expiry_date and self.expiry_date < timezone.localdate():
            return "Expired"
        return self.get_status_display()

    def __str__(self):
        return self.title


class BusinessDocumentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        BusinessDocument, on_delete=models.PROTECT, related_name="versions"
    )
    file = models.FileField(upload_to=business_document_path, max_length=512)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=127)
    size_bytes = models.PositiveBigIntegerField()
    version_number = models.PositiveIntegerField()
    metadata_snapshot = models.JSONField(default=dict)
    change_note = models.CharField(max_length=500, blank=True)
    is_current = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="business_document_versions",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-version_number",)
        constraints = [
            models.UniqueConstraint(
                fields=("document", "version_number"),
                name="unique_business_document_version_number",
            ),
            models.UniqueConstraint(
                fields=("document",),
                condition=Q(is_current=True),
                name="unique_current_business_document_version",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise PermissionDenied("Business document versions are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Business document versions are retained as history.")


class FounderProfile(models.Model):
    class Gender(models.TextChoices):
        WOMAN = "woman", "Woman"
        MAN = "man", "Man"
        NON_BINARY = "non_binary", "Non-binary"
        SELF_DESCRIBED = "self_described", "Self-described"
        PREFER_NOT_TO_SAY = "prefer_not_to_say", "Prefer not to say"

    class Race(models.TextChoices):
        AFRICAN = "african", "African"
        COLOURED = "coloured", "Coloured"
        INDIAN = "indian", "Indian"
        WHITE = "white", "White"
        OTHER = "other", "Other"
        PREFER_NOT_TO_SAY = "prefer_not_to_say", "Prefer not to say"

    stakeholder = models.OneToOneField(
        Stakeholder, on_delete=models.PROTECT, related_name="founder_profile"
    )
    id_number_encrypted = models.TextField(blank=True)
    id_number_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    gender = models.CharField(max_length=24, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    race = models.CharField(max_length=24, choices=Race.choices, blank=True)
    disability_status = models.CharField(
        max_length=24, choices=Incubatee.DisabilityStatus.choices, blank=True
    )
    highest_qualification = models.CharField(max_length=255, blank=True)
    prior_business_experience = models.TextField(blank=True)


class IncubateeFounder(models.Model):
    incubatee = models.ForeignKey(Incubatee, on_delete=models.PROTECT, related_name="founders")
    founder = models.ForeignKey(FounderProfile, on_delete=models.PROTECT, related_name="businesses")
    ownership_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ended_incubatee_founder_links",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("incubatee", "founder"),
                condition=Q(is_active=True),
                name="unique_active_founder_per_incubatee",
            ),
            models.CheckConstraint(
                condition=Q(ownership_percentage__isnull=True)
                | Q(ownership_percentage__gte=0, ownership_percentage__lte=100),
                name="founder_ownership_0_to_100",
            ),
        ]


class IncubateeConsent(models.Model):
    class Purpose(models.TextChoices):
        POPIA = "popia", "POPIA processing consent"
        FUNDER_REPORTING = "funder_reporting", "Data sharing for funder reporting"

    incubatee = models.ForeignKey(Incubatee, on_delete=models.PROTECT, related_name="consent_history")
    purpose = models.CharField(max_length=32, choices=Purpose.choices)
    status = models.CharField(max_length=16, choices=Incubatee.ConsentStatus.choices)
    consent_date = models.DateField()
    method = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recorded_incubatee_consents"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-consent_date", "-created_at")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Incubatee consent records cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Incubatee consent records cannot be deleted.")


class IncubateeBaselineSnapshot(models.Model):
    incubatee = models.ForeignKey(Incubatee, on_delete=models.PROTECT, related_name="baseline_history")
    snapshot = models.JSONField()
    founder_snapshot = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=64)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="incubatee_baseline_changes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def save(self, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise PermissionDenied("Incubatee baseline snapshots are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied("Incubatee baseline snapshots are immutable.")


class IncubateeStatusHistory(models.Model):
    incubatee = models.ForeignKey(Incubatee, on_delete=models.PROTECT, related_name="status_history")
    old_status = models.CharField(max_length=16, choices=Incubatee.Status.choices, blank=True)
    new_status = models.CharField(max_length=16, choices=Incubatee.Status.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="incubatee_status_changes"
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")

    def save(self, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise PermissionDenied("Incubatee status history is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied("Incubatee status history is immutable.")


class IncubateeDocument(models.Model):
    class Category(models.TextChoices):
        CIPC_CERTIFICATE = "cipc_certificate", "CIPC certificate"
        TAX_CLEARANCE = "tax_clearance", "Tax clearance"
        ID_COPY = "id_copy", "ID copy"
        BUSINESS_PLAN = "business_plan", "Business plan"
        FINANCIAL_DOCUMENT = "financial_document", "Financial document"
        BANK_STATEMENT = "bank_statement", "Bank statement"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    incubatee = models.ForeignKey(Incubatee, on_delete=models.PROTECT, related_name="documents")
    category = models.CharField(max_length=32, choices=Category.choices)
    title = models.CharField(max_length=255)
    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to=incubatee_document_path, max_length=512)
    content_type = models.CharField(max_length=127)
    size_bytes = models.PositiveBigIntegerField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_incubatee_documents"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("category", "title", "uploaded_at")


class IncubateeAccessEvent(models.Model):
    class Action(models.TextChoices):
        PERSONAL_DATA_VIEW = "personal_data_view", "Personal data viewed"
        DOCUMENT_DOWNLOAD = "document_download", "Incubatee document downloaded"
        CHECKIN_LINK_VIEW = "checkin_link_view", "Check-in link viewed"
        CHECKIN_SUBMITTED = "checkin_submitted", "Check-in submitted"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="incubatee_access_events",
    )
    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="access_events"
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    resource = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-occurred_at",)

    def save(self, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise PermissionDenied("Incubatee access events are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied("Incubatee access events are immutable.")


def incubatee_evidence_path(instance, filename):
    extension = Path(filename).suffix.lower()[:16]
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in extension):
        extension = ""
    file_id = instance.pk or uuid.uuid4()
    return f"incubatees/{instance.incubatee_id}/evidence/{file_id}{extension}"


class MonthlyCheckIn(models.Model):
    incubatee = models.ForeignKey(Incubatee, on_delete=models.PROTECT, related_name="monthly_checkins")
    period_start = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_incubatee_checkins",
    )
    completeness_score = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-period_start", "incubatee__legal_name")
        constraints = [
            models.UniqueConstraint(
                fields=("incubatee", "period_start"),
                name="unique_monthly_checkin_period",
            ),
            models.CheckConstraint(
                condition=Q(period_start__day=1), name="checkin_period_month_start"
            ),
            models.CheckConstraint(
                condition=Q(completeness_score__isnull=True)
                | Q(completeness_score__gte=0, completeness_score__lte=100),
                name="checkin_completeness_0_to_100",
            ),
        ]


class CheckInAccessLink(models.Model):
    checkin = models.ForeignKey(
        MonthlyCheckIn, on_delete=models.PROTECT, related_name="access_links"
    )
    recipient_email = models.EmailField()
    token_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("checkin", "recipient_email"),
                name="unique_checkin_link_recipient",
            )
        ]


class CheckInReminder(models.Model):
    access_link = models.ForeignKey(
        CheckInAccessLink, on_delete=models.PROTECT, related_name="reminders"
    )
    days_before_due = models.PositiveSmallIntegerField()
    sent_at = models.DateTimeField(null=True, blank=True)
    email_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("access_link", "days_before_due"),
                name="unique_checkin_reminder_interval",
            )
        ]


class QuarterlyReview(models.Model):
    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="quarterly_reviews"
    )
    period_start = models.DateField()
    tax_clearance_status = models.CharField(max_length=32, blank=True)
    cipc_annual_return_status = models.CharField(max_length=32, blank=True)
    management_accounts = models.FileField(upload_to=incubatee_evidence_path, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=127, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_quarterly_reviews",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=64, default="staff_form")

    class Meta:
        ordering = ("-period_start",)
        constraints = [
            models.UniqueConstraint(
                fields=("incubatee", "period_start"),
                name="unique_quarterly_review_period",
            ),
            models.CheckConstraint(
                condition=Q(period_start__day=1)
                & (
                    Q(period_start__month=1)
                    | Q(period_start__month=4)
                    | Q(period_start__month=7)
                    | Q(period_start__month=10)
                ),
                name="quarterly_review_quarter_start",
            ),
        ]


class IncubateeMetricObservation(models.Model):
    class PeriodType(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"

    class Availability(models.TextChoices):
        RECORDED = "recorded", "Recorded"
        NOT_APPLICABLE = "not_applicable", "Not applicable"

    class Metric(models.TextChoices):
        REVENUE_ZAR = "revenue_zar", "Revenue this month (ZAR)"
        CUSTOMER_COUNT = "customer_count", "Number of customers"
        NEW_CUSTOMERS = "new_customers", "New customers this month"
        FULL_TIME_EMPLOYEES = "full_time_employees", "Full-time employees"
        PART_TIME_EMPLOYEES = "part_time_employees", "Part-time employees"
        FULL_TIME_MEN = "full_time_men", "Full-time men"
        FULL_TIME_WOMEN = "full_time_women", "Full-time women"
        FULL_TIME_YOUTH = "full_time_youth", "Full-time youth"
        PART_TIME_MEN = "part_time_men", "Part-time men"
        PART_TIME_WOMEN = "part_time_women", "Part-time women"
        PART_TIME_YOUTH = "part_time_youth", "Part-time youth"
        JOBS_CREATED = "jobs_created", "Jobs created"
        JOBS_LOST = "jobs_lost", "Jobs lost"
        FUNDING_APPLIED_ZAR = "funding_applied_zar", "Funding applied for (ZAR)"
        FUNDING_RECEIVED_ZAR = "funding_received_zar", "Funding received (ZAR)"
        FUNDING_SOURCE = "funding_source", "Funding source this month"
        BUSINESS_STAGE = "business_stage", "Business stage this month"
        BIGGEST_WIN = "biggest_win", "Biggest win"
        BIGGEST_CHALLENGE = "biggest_challenge", "Biggest challenge"
        CHALLENGE_CATEGORY = "challenge_category", "Challenge category"
        SUPPORT_NEEDED = "support_needed", "Support needed next month"
        CONFIDENCE_SCORE = "confidence_score", "Confidence score"
        FINANCE_SCORE = "finance_score", "Finance self-assessment"
        MARKETING_SCORE = "marketing_score", "Marketing self-assessment"
        OPERATIONS_SCORE = "operations_score", "Operations self-assessment"
        COMPLIANCE_SCORE = "compliance_score", "Compliance self-assessment"
        TECHNOLOGY_SCORE = "technology_score", "Technology self-assessment"
        LEADERSHIP_SCORE = "leadership_score", "Leadership self-assessment"
        TAX_CLEARANCE_STATUS = "tax_clearance_status", "Tax clearance status"
        CIPC_RETURN_STATUS = "cipc_return_status", "CIPC annual return status"

    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="metric_observations"
    )
    period_type = models.CharField(max_length=16, choices=PeriodType.choices)
    period_start = models.DateField()
    metric = models.CharField(max_length=32, choices=Metric.choices)
    availability = models.CharField(
        max_length=24, choices=Availability.choices, default=Availability.RECORDED
    )
    decimal_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    integer_value = models.BigIntegerField(null=True, blank=True)
    text_value = models.TextField(null=True, blank=True)
    boolean_value = models.BooleanField(null=True, blank=True)
    source = models.CharField(max_length=64)
    monthly_checkin = models.ForeignKey(
        MonthlyCheckIn,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    quarterly_review = models.ForeignKey(
        QuarterlyReview,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recorded_incubatee_metrics",
    )
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-period_start", "metric")
        constraints = [
            models.UniqueConstraint(
                fields=("incubatee", "period_type", "period_start", "metric"),
                name="unique_incubatee_period_metric",
            ),
            models.CheckConstraint(
                condition=Q(
                    availability="not_applicable",
                    decimal_value__isnull=True,
                    integer_value__isnull=True,
                    text_value__isnull=True,
                    boolean_value__isnull=True,
                )
                | (
                    Q(
                        availability="recorded",
                        decimal_value__isnull=False,
                        integer_value__isnull=True,
                        text_value__isnull=True,
                        boolean_value__isnull=True,
                    )
                    | Q(
                        availability="recorded",
                        decimal_value__isnull=True,
                        integer_value__isnull=False,
                        text_value__isnull=True,
                        boolean_value__isnull=True,
                    )
                    | Q(
                        availability="recorded",
                        decimal_value__isnull=True,
                        integer_value__isnull=True,
                        text_value__isnull=False,
                        boolean_value__isnull=True,
                    )
                    | Q(
                        availability="recorded",
                        decimal_value__isnull=True,
                        integer_value__isnull=True,
                        text_value__isnull=True,
                        boolean_value__isnull=False,
                    )
                ),
                name="metric_value_matches_availability",
            ),
            models.CheckConstraint(
                condition=Q(decimal_value__isnull=True) | Q(decimal_value__gte=0),
                name="metric_decimal_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(integer_value__isnull=True) | Q(integer_value__gte=0),
                name="metric_integer_nonnegative",
            ),
            models.CheckConstraint(
                condition=~Q(metric="confidence_score")
                | Q(availability="not_applicable")
                | Q(integer_value__gte=1, integer_value__lte=10),
                name="confidence_metric_1_to_10",
            ),
            models.CheckConstraint(
                condition=~Q(
                    metric__in=(
                        "finance_score",
                        "marketing_score",
                        "operations_score",
                        "compliance_score",
                        "technology_score",
                        "leadership_score",
                    )
                )
                | Q(availability="not_applicable")
                | Q(integer_value__gte=1, integer_value__lte=5),
                name="quarterly_assessment_score_1_to_5",
            ),
        ]

    def clean(self):
        super().clean()
        if self.period_type == self.PeriodType.MONTHLY and self.period_start.day != 1:
            raise ValidationError({"period_start": "Monthly observations use the first day of the month."})
        if self.period_type == self.PeriodType.QUARTERLY and (
            self.period_start.day != 1 or self.period_start.month not in (1, 4, 7, 10)
        ):
            raise ValidationError({"period_start": "Quarterly observations use the first day of a quarter."})
        if self.metric == self.Metric.CONFIDENCE_SCORE and self.integer_value is not None:
            if not 1 <= self.integer_value <= 10:
                raise ValidationError({"integer_value": "Confidence score must be between 1 and 10."})
        if self.metric in {
            self.Metric.FINANCE_SCORE,
            self.Metric.MARKETING_SCORE,
            self.Metric.OPERATIONS_SCORE,
            self.Metric.COMPLIANCE_SCORE,
            self.Metric.TECHNOLOGY_SCORE,
            self.Metric.LEADERSHIP_SCORE,
        } and self.integer_value is not None:
            if not 1 <= self.integer_value <= 5:
                raise ValidationError({"integer_value": "Quarterly assessment scores must be between 1 and 5."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Metric observations are immutable; submit a new period instead.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Metric observations are retained as historical records.")


class IncubateeObservationEvidence(models.Model):
    observation = models.ForeignKey(
        IncubateeMetricObservation, on_delete=models.PROTECT, related_name="evidence"
    )
    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="metric_evidence"
    )
    title = models.CharField(max_length=255)
    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to=incubatee_evidence_path, max_length=512)
    content_type = models.CharField(max_length=127)
    size_bytes = models.PositiveBigIntegerField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_metric_evidence",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-uploaded_at",)


class MonthlyCheckInEvidence(models.Model):
    checkin = models.ForeignKey(
        MonthlyCheckIn, on_delete=models.PROTECT, related_name="evidence"
    )
    observation = models.ForeignKey(
        IncubateeMetricObservation,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="checkin_evidence",
    )
    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="checkin_evidence"
    )
    title = models.CharField(max_length=255)
    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to=incubatee_evidence_path, max_length=512)
    content_type = models.CharField(max_length=127)
    size_bytes = models.PositiveBigIntegerField()
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-uploaded_at",)


class IncubateeMilestone(models.Model):
    class Type(models.TextChoices):
        BUSINESS_IDEA_DEVELOPED = "business_idea_developed", "Business idea developed"
        BUSINESS_REGISTRATION = "business_registration", "Business registration completed"
        PRODUCT_DEVELOPED = "product_developed", "Product developed"
        FIRST_CUSTOMER = "first_customer", "First customer acquired"
        FIRST_SALE = "first_sale", "First sale"
        FIRST_HIRE = "first_hire", "First hire"
        FUNDING_RAISED = "funding_raised", "Funding raised"
        NEW_CONTRACT = "new_contract", "New contract"
        AWARD = "award", "Award"
        PRODUCT_LAUNCH = "product_launch", "Product launch"
        NEW_MARKET = "new_market", "New market entered"
        OTHER_ACHIEVEMENT = "other_achievement", "Other business achievement"

    title = models.CharField(max_length=255, blank=True)
    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="milestones"
    )
    milestone_type = models.CharField(max_length=32, choices=Type.choices)
    milestone_date = models.DateField()
    value_zar = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    description = models.TextField(blank=True)
    evidence = models.FileField(upload_to=incubatee_evidence_path, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=127, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    source = models.CharField(max_length=64, default="staff_form")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_incubatee_milestones",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-milestone_date", "-created_at")
        constraints = [
            models.CheckConstraint(
                condition=Q(value_zar__isnull=True) | Q(value_zar__gte=0),
                name="milestone_value_nonnegative",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Milestone records are immutable.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Milestone records are retained as historical records.")

    @property
    def review_status(self):
        latest_review = self.reviews.first()
        return latest_review.status if latest_review else "pending"


class IncubateeProgressSubmission(models.Model):
    checkin = models.OneToOneField(
        MonthlyCheckIn, on_delete=models.PROTECT, related_name="portal_submission"
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_progress_submissions",
    )
    business_achievements = models.TextField(blank=True)
    challenges_experienced = models.TextField(blank=True)
    additional_comments = models.TextField(blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-checkin__period_start", "-submitted_at")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and any(
                getattr(previous, field) != getattr(self, field)
                for field in (
                    "checkin_id",
                    "submitted_by_id",
                    "business_achievements",
                    "challenges_experienced",
                    "additional_comments",
                    "submitted_at",
                )
            ):
                raise PermissionDenied("Submitted progress content is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Progress submissions are retained as history.")

    @property
    def review_status(self):
        latest_review = self.reviews.first()
        return latest_review.status if latest_review else "submitted"

    @property
    def review_note(self):
        latest_review = self.reviews.first()
        return latest_review.review_note if latest_review else ""

    @property
    def review_status_display(self):
        latest_review = self.reviews.first()
        return latest_review.get_status_display() if latest_review else "Submitted"


class IncubateeProgressReview(models.Model):
    class Status(models.TextChoices):
        REVIEWED = "reviewed", "Reviewed"
        CLARIFICATION_REQUESTED = "clarification_requested", "Clarification requested"

    submission = models.ForeignKey(
        IncubateeProgressSubmission, on_delete=models.PROTECT, related_name="reviews"
    )
    status = models.CharField(max_length=32, choices=Status.choices)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_progress_reviews",
    )
    review_note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-reviewed_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionDenied("Progress review history is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Progress review history is retained.")


class IncubateeMilestoneReview(models.Model):
    class Status(models.TextChoices):
        APPROVED = "approved", "Reviewed"
        CLARIFICATION_REQUESTED = "clarification_requested", "Clarification requested"
        NOT_VERIFIED = "not_verified", "Not verified"

    milestone = models.ForeignKey(
        IncubateeMilestone, on_delete=models.PROTECT, related_name="reviews"
    )
    status = models.CharField(max_length=32, choices=Status.choices)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_milestone_reviews",
    )
    review_note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-reviewed_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionDenied("Milestone review history is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Milestone review history is retained.")


def challenge_evidence_path(instance, filename):
    extension = Path(filename).suffix.lower()[:16]
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in extension):
        extension = ""
    return f"incubatees/{instance.incubatee_id}/challenges/{uuid.uuid4()}{extension}"


class IncubateeBusinessChallenge(models.Model):
    class Category(models.TextChoices):
        FUNDING = "funding", "Lack of funding"
        MARKETING = "marketing", "Marketing difficulties"
        EQUIPMENT = "equipment", "Equipment shortages"
        COMPLIANCE = "compliance", "Compliance problems"
        OPERATIONS = "operations", "Operational challenges"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"

    description = models.TextField()
    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="business_challenges"
    )
    category = models.CharField(max_length=24, choices=Category.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_incubatee_challenges",
    )
    reported_at = models.DateTimeField(auto_now_add=True)
    evidence = models.FileField(upload_to=challenge_evidence_path, blank=True)
    evidence_original_filename = models.CharField(max_length=255, blank=True)
    evidence_content_type = models.CharField(max_length=127, blank=True)
    evidence_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ("-reported_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and any(
                getattr(previous, field) != getattr(self, field)
                for field in (
                    "description",
                    "incubatee_id",
                    "category",
                    "reported_by_id",
                    "reported_at",
                    "evidence",
                    "evidence_original_filename",
                    "evidence_content_type",
                    "evidence_size_bytes",
                )
            ):
                raise PermissionDenied("Business challenge content is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Business challenge submissions are retained.")

    @property
    def review_status(self):
        latest_review = self.reviews.first()
        return latest_review.status if latest_review else "pending"


class IncubateeChallengeReview(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"

    challenge = models.ForeignKey(
        IncubateeBusinessChallenge, on_delete=models.PROTECT, related_name="reviews"
    )
    status = models.CharField(max_length=32, choices=Status.choices)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_challenge_reviews",
    )
    review_note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-reviewed_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionDenied("Challenge review history is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Challenge review history is retained.")


class IncubateeSupportRequest(models.Model):
    class Category(models.TextChoices):
        MENTORSHIP = "mentorship", "Business mentorship"
        FINANCIAL_MANAGEMENT = "financial_management", "Financial management assistance"
        MARKETING = "marketing", "Marketing support"
        REGISTRATION_COMPLIANCE = "registration_compliance", "Business registration and compliance"
        FUNDING_GUIDANCE = "funding_guidance", "Funding application guidance"
        TECHNOLOGY = "technology", "Technology support"
        TRAINING = "training", "Training and workshops"
        OTHER = "other", "Other business development assistance"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        UNDER_REVIEW = "under_review", "Under Review"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        REJECTED = "rejected", "Rejected"

    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="support_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_support_requests",
    )
    category = models.CharField(max_length=32, choices=Category.choices)
    description = models.TextField()
    priority = models.CharField(max_length=16, choices=Priority.choices, blank=True)
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.SUBMITTED
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assigned_incubatee_support_requests",
    )
    evidence = models.FileField(
        upload_to=support_request_evidence_path, max_length=512, blank=True
    )
    evidence_original_filename = models.CharField(max_length=255, blank=True)
    evidence_content_type = models.CharField(max_length=127, blank=True)
    evidence_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-submitted_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and any(
                getattr(previous, field) != getattr(self, field)
                for field in (
                    "incubatee_id",
                    "requested_by_id",
                    "category",
                    "description",
                    "priority",
                    "evidence",
                    "evidence_original_filename",
                    "evidence_content_type",
                    "evidence_size_bytes",
                    "submitted_at",
                )
            ):
                raise PermissionDenied("Support request details are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Support requests are retained as history.")


class IncubateeSupportRequestUpdate(models.Model):
    support_request = models.ForeignKey(
        IncubateeSupportRequest, on_delete=models.PROTECT, related_name="updates"
    )
    status = models.CharField(
        max_length=24,
        choices=IncubateeSupportRequest.Status.choices,
        blank=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="incubatee_support_request_assignments",
    )
    response = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_support_request_updates",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionDenied("Support request updates are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Support request updates are retained.")


class IncubateeBusinessDevelopmentNeedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionDenied("Business development needs must be updated with history.")

    def delete(self):
        raise PermissionDenied("Business development needs are retained as history.")


class IncubateeBusinessDevelopmentNeed(models.Model):
    class Category(models.TextChoices):
        BUSINESS_IDEA = "business_idea", "Need a business idea"
        REFINE_IDEA = "refine_idea", "Refine an existing business idea"
        REGISTRATION = "registration", "Business registration assistance"
        MARKETING = "marketing", "Marketing products or services"
        PRODUCT_DEVELOPMENT = "product_development", "Product development"
        FUNDING = "funding", "Funding assistance"
        FINANCIAL_MANAGEMENT = "financial_management", "Financial management"
        TECHNOLOGY = "technology", "Technology and digital transformation"
        MENTORSHIP = "mentorship", "Business mentorship"
        MARKET_ACCESS = "market_access", "Market access"
        COMPLIANCE = "compliance", "Business compliance"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"

    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="business_development_needs"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submitted_business_development_needs",
    )
    category = models.CharField(max_length=32, choices=Category.choices)
    description = models.TextField(blank=True)
    identified_at = models.DateField(default=timezone.localdate, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assigned_business_development_needs",
    )
    resolution_date = models.DateField(null=True, blank=True)
    evidence = models.FileField(
        upload_to=business_development_need_evidence_path, max_length=512, blank=True
    )
    evidence_original_filename = models.CharField(max_length=255, blank=True)
    evidence_content_type = models.CharField(max_length=127, blank=True)
    evidence_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    support_request = models.OneToOneField(
        IncubateeSupportRequest,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="business_development_need",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("status", "-identified_at", "-pk")

    objects = IncubateeBusinessDevelopmentNeedQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and any(
                getattr(previous, field) != getattr(self, field)
                for field in (
                    "incubatee_id",
                    "requested_by_id",
                    "category",
                    "description",
                    "identified_at",
                    "evidence",
                    "evidence_original_filename",
                    "evidence_content_type",
                    "evidence_size_bytes",
                    "support_request_id",
                    "created_at",
                )
            ):
                raise PermissionDenied("Business development need details are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Business development needs are retained as history.")


class IncubateeBusinessDevelopmentNeedUpdateQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionDenied("Business development need updates are immutable.")

    def delete(self):
        raise PermissionDenied("Business development need updates are retained.")


class IncubateeBusinessDevelopmentNeedUpdate(models.Model):
    need = models.ForeignKey(
        IncubateeBusinessDevelopmentNeed,
        on_delete=models.PROTECT,
        related_name="updates",
    )
    status = models.CharField(
        max_length=16, choices=IncubateeBusinessDevelopmentNeed.Status.choices
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="business_development_need_updates",
    )
    support_provided = models.TextField(blank=True)
    outcome = models.TextField(blank=True)
    resolution_date = models.DateField(null=True, blank=True)
    evidence = models.FileField(
        upload_to=business_development_evidence_path, max_length=512, blank=True
    )
    evidence_original_filename = models.CharField(max_length=255, blank=True)
    evidence_content_type = models.CharField(max_length=127, blank=True)
    evidence_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    mentoring_session = models.ForeignKey(
        "incubation.MentoringSession",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="business_development_need_updates",
    )
    interaction = models.ForeignKey(
        "interactions.Interaction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="business_development_need_updates",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_business_development_need_updates",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    objects = IncubateeBusinessDevelopmentNeedUpdateQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionDenied("Business development need updates are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Business development need updates are retained.")


class IncubateePortalNotification(models.Model):
    class Type(models.TextChoices):
        SUPPORT_REQUEST = "support_request", "Support request update"
        PROFILE_CHANGE = "profile_change", "Business profile change"
        PROGRESS_DEADLINE = "progress_deadline", "Monthly progress"
        DOCUMENT_EXPIRY = "document_expiry", "Business document expiry"
        MENTORSHIP = "mentorship", "Mentorship"
        ACCOUNT = "account", "Account"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="incubatee_portal_notifications",
    )
    incubatee = models.ForeignKey(
        Incubatee,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="portal_notifications",
    )
    notification_type = models.CharField(max_length=24, choices=Type.choices)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_attempted_at = models.DateTimeField(null=True, blank=True)
    email_error = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and any(
                getattr(previous, field) != getattr(self, field)
                for field in (
                    "user_id",
                    "incubatee_id",
                    "notification_type",
                    "title",
                    "message",
                    "created_at",
                )
            ):
                raise PermissionDenied("Notification content is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Notifications are retained as history.")


class MentoringSession(models.Model):
    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="mentoring_sessions"
    )
    mentor = models.ForeignKey(
        Stakeholder,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="incubatee_sessions",
    )
    session_date = models.DateField()
    mentoring_hours = models.DecimalField(max_digits=6, decimal_places=2)
    topics = models.TextField(blank=True)
    progress_rating = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=64, default="staff_form")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_mentoring_sessions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-session_date", "-created_at")
        constraints = [
            models.CheckConstraint(
                condition=Q(mentoring_hours__gt=0), name="mentoring_hours_positive"
            ),
            models.CheckConstraint(
                condition=Q(progress_rating__isnull=True) | Q(progress_rating__gte=0),
                name="mentoring_rating_nonnegative",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Mentoring session records are immutable.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Mentoring session records are retained as history.")


class IncubateeExit(models.Model):
    class Outcome(models.TextChoices):
        GRADUATED = "graduated", "Graduated"
        EXITED = "exited", "Exited"
        WITHDRAWN = "withdrawn", "Withdrawn"

    incubatee = models.ForeignKey(
        Incubatee, on_delete=models.PROTECT, related_name="exit_records"
    )
    exit_date = models.DateField()
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    reason = models.TextField(blank=True)
    outcomes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_incubatee_exits",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-exit_date", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("incubatee", "exit_date"), name="unique_incubatee_exit_date"
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Exit records are immutable.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Exit records are retained as history.")


class IncubateeFollowUp(models.Model):
    class Status(models.TextChoices):
        OPERATING = "operating", "Operating"
        NOT_OPERATING = "not_operating", "Not operating"
        UNKNOWN = "unknown", "Unknown"

    exit_record = models.ForeignKey(
        IncubateeExit, on_delete=models.PROTECT, related_name="followups"
    )
    months_after_exit = models.PositiveSmallIntegerField()
    due_date = models.DateField()
    submitted_at = models.DateTimeField(null=True, blank=True)
    business_status = models.CharField(max_length=24, choices=Status.choices, blank=True)
    revenue_zar = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    full_time_employees = models.PositiveIntegerField(null=True, blank=True)
    part_time_employees = models.PositiveIntegerField(null=True, blank=True)
    funding_received_zar = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_incubatee_followups",
    )

    class Meta:
        ordering = ("due_date",)
        constraints = [
            models.UniqueConstraint(
                fields=("exit_record", "months_after_exit"),
                name="unique_exit_followup_interval",
            ),
            models.CheckConstraint(
                condition=Q(months_after_exit__in=(6, 12)),
                name="followup_at_6_or_12_months",
            ),
            models.CheckConstraint(
                condition=Q(revenue_zar__isnull=True) | Q(revenue_zar__gte=0),
                name="followup_revenue_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(funding_received_zar__isnull=True) | Q(funding_received_zar__gte=0),
                name="followup_funding_nonnegative",
            ),
        ]


class ProgrammeCostObservation(models.Model):
    programme = models.ForeignKey(
        Programme, on_delete=models.PROTECT, related_name="cost_observations"
    )
    period_start = models.DateField()
    amount_zar = models.DecimalField(max_digits=16, decimal_places=2)
    source = models.CharField(max_length=64)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_programme_costs",
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-period_start",)
        constraints = [
            models.UniqueConstraint(
                fields=("programme", "period_start"),
                name="unique_programme_cost_period",
            ),
            models.CheckConstraint(
                condition=Q(amount_zar__gte=0), name="programme_cost_nonnegative"
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Programme cost observations are immutable.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Programme cost observations are retained as history.")
