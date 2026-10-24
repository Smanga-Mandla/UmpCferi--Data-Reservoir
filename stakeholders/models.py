from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.db.models import Q

from accounts.models import AccessControlledModel, RoleGrant


class Stakeholder(AccessControlledModel):
    access_resource = RoleGrant.Resource.STAKEHOLDERS

    class RelationshipType(models.TextChoices):
        BOARD_MEMBER = "board_member", "Board Member"
        STAFF = "staff", "Staff"
        FREELANCER = "freelancer", "Freelancer"
        PARTNER_REPRESENTATIVE = "partner_representative", "Partner Representative"
        GOVERNMENT_OFFICIAL = "government_official", "Government Official"
        PRIVATE_SECTOR_CONTACT = "private_sector_contact", "Private Sector Contact"
        FOUNDER = "founder", "Founder"

    name = models.CharField(max_length=255)
    title = models.CharField(max_length=255, blank=True)
    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_stakeholders",
    )

    class Meta:
        ordering = ("name", "email")
        verbose_name = "stakeholder"
        verbose_name_plural = "stakeholders"

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower() or None
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class StakeholderTypeAssignment(models.Model):
    stakeholder = models.ForeignKey(
        Stakeholder,
        on_delete=models.PROTECT,
        related_name="type_assignments",
    )
    relationship_type = models.CharField(
        max_length=32,
        choices=Stakeholder.RelationshipType.choices,
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_stakeholder_types",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ended_stakeholder_types",
    )
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("relationship_type", "assigned_at")
        constraints = [
            models.UniqueConstraint(
                fields=("stakeholder", "relationship_type"),
                condition=Q(is_active=True),
                name="unique_active_stakeholder_type",
            ),
        ]

    def __str__(self):
        return f"{self.stakeholder}: {self.get_relationship_type_display()}"


class BoardMemberProfile(models.Model):
    stakeholder = models.OneToOneField(
        Stakeholder,
        on_delete=models.PROTECT,
        related_name="board_profile",
    )
    appointment_date = models.DateField(null=True, blank=True)
    term_end = models.DateField(null=True, blank=True)
    committees = models.TextField(blank=True)
    declarations_of_interest = models.TextField(blank=True)


class StaffProfile(models.Model):
    stakeholder = models.OneToOneField(
        Stakeholder,
        on_delete=models.PROTECT,
        related_name="staff_profile",
    )
    position = models.CharField(max_length=255, blank=True)
    contract_start = models.DateField(null=True, blank=True)
    contract_end = models.DateField(null=True, blank=True)
    skills = models.TextField(blank=True)
    @property
    def assigned_incubatees(self):
        from incubation.models import Incubatee

        return Incubatee.objects.filter(
            staff_assignments__staff_profile=self,
            staff_assignments__is_active=True,
        ).distinct()


class StaffIncubateeAssignmentQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionDenied("Staff-incubatee assignment history is retained.")

    def delete(self):
        raise PermissionDenied("Staff-incubatee assignment history is retained.")


class StaffIncubateeAssignment(models.Model):
    staff_profile = models.ForeignKey(
        StaffProfile,
        on_delete=models.PROTECT,
        related_name="incubatee_assignments",
    )
    incubatee = models.ForeignKey(
        "incubation.Incubatee",
        on_delete=models.PROTECT,
        related_name="staff_assignments",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_stakeholder_incubatees",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, db_index=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ended_stakeholder_incubatee_assignments",
    )
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-assigned_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("staff_profile", "incubatee"),
                condition=Q(is_active=True),
                name="unique_active_staff_incubatee_assignment",
            ),
        ]

    objects = StaffIncubateeAssignmentQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "staff_profile_id",
                "incubatee_id",
                "assigned_by_id",
                "assigned_at",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Assignment history details cannot be changed.")
            if not previous.is_active or self.is_active:
                raise ValidationError("Only an active assignment may be ended.")
            if not self.ended_by_id or not self.ended_at:
                raise ValidationError("Ending an assignment requires an actor and timestamp.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Staff-incubatee assignment history is retained.")


class POPIAConsent(models.Model):
    stakeholder = models.ForeignKey(
        Stakeholder,
        on_delete=models.PROTECT,
        related_name="consent_history",
    )
    consent_date = models.DateField()
    purpose = models.TextField()
    method = models.CharField(max_length=255)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_stakeholder_consents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-consent_date", "-created_at")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Consent history entries cannot be edited.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Consent history entries cannot be deleted.")
