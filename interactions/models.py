from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.db.models import Q

from accounts.models import AccessControlledModel, RoleGrant
from stakeholders.models import Stakeholder


class Interaction(AccessControlledModel):
    access_resource = RoleGrant.Resource.INTERACTIONS

    class Type(models.TextChoices):
        MEETING = "meeting", "Meeting"
        MENTORING = "mentoring", "Mentoring"
        WORKSHOP = "workshop", "Workshop"
        SITE_VISIT = "site_visit", "Site visit"
        CALL = "call", "Call"
        EMAIL = "email", "Email"
        EVENT = "event", "Event"

    occurred_at = models.DateTimeField()
    duration_minutes = models.PositiveIntegerField()
    interaction_type = models.CharField(max_length=16, choices=Type.choices)
    participants = models.ManyToManyField(
        Stakeholder,
        through="InteractionParticipant",
        related_name="interactions",
        blank=True,
    )
    incubatees = models.ManyToManyField(
        "incubation.Incubatee",
        related_name="interactions",
        blank=True,
    )
    partners = models.ManyToManyField(
        "partners.Partner",
        related_name="interactions",
        blank=True,
    )
    topics = models.TextField(blank=True)
    outcome = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_interactions",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="updated_interactions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-occurred_at", "-pk")
        constraints = [
            models.CheckConstraint(
                condition=Q(duration_minutes__gte=0),
                name="interaction_duration_nonnegative",
            )
        ]

    def __str__(self):
        return f"{self.get_interaction_type_display()} — {self.occurred_at:%Y-%m-%d %H:%M}"


class InteractionParticipant(models.Model):
    interaction = models.ForeignKey(
        Interaction, on_delete=models.PROTECT, related_name="participant_links"
    )
    stakeholder = models.ForeignKey(
        Stakeholder,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="interaction_links",
    )
    display_name = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("interaction", "stakeholder"),
                condition=Q(stakeholder__isnull=False),
                name="unique_interaction_participant",
            )
        ]

    def clean(self):
        if self.stakeholder_id and self.display_name:
            raise ValidationError(
                {"display_name": "Use a stakeholder record or an unlinked participant name."}
            )
        if not self.stakeholder_id and not self.display_name.strip():
            raise ValidationError(
                {"display_name": "Enter a name when no stakeholder record is selected."}
            )


class InteractionHistoryQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionDenied("Interaction history entries are immutable.")

    def delete(self):
        raise PermissionDenied("Interaction history entries are immutable.")


class InteractionHistory(models.Model):
    class ChangeType(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        DELETED = "deleted", "Deleted"

    interaction = models.ForeignKey(
        Interaction, on_delete=models.PROTECT, related_name="history"
    )
    change_type = models.CharField(max_length=16, choices=ChangeType.choices)
    snapshot = models.JSONField()
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="interaction_history_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = InteractionHistoryQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-pk")

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise PermissionDenied("Interaction history entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Interaction history entries are immutable.")


class FollowUpTask(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"

    interaction = models.ForeignKey(
        Interaction, on_delete=models.PROTECT, related_name="follow_up_tasks"
    )
    description = models.CharField(max_length=500)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="interaction_follow_up_tasks",
    )
    due_date = models.DateField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_interaction_tasks",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("status", "due_date", "pk")

    def __str__(self):
        return self.description


class EventAttendance(models.Model):
    class Status(models.TextChoices):
        REGISTERED = "registered", "Registered"
        ATTENDED = "attended", "Attended"
        ABSENT = "absent", "Absent"

    interaction = models.ForeignKey(
        Interaction, on_delete=models.PROTECT, related_name="attendance"
    )
    stakeholder = models.ForeignKey(
        Stakeholder,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="event_attendance",
    )
    participant_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.REGISTERED
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_event_attendance",
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("stakeholder__name",)
        constraints = [
            models.UniqueConstraint(
                fields=("interaction", "stakeholder"),
                condition=Q(stakeholder__isnull=False),
                name="unique_interaction_attendance",
            )
        ]

    def clean(self):
        if self.stakeholder_id and self.participant_name:
            raise ValidationError(
                {"participant_name": "Use a stakeholder record or enter a standalone name."}
            )
        if not self.stakeholder_id and not self.participant_name.strip():
            raise ValidationError(
                {"participant_name": "Enter a name or select a stakeholder."}
            )
        if self.interaction_id and self.stakeholder_id and not self.interaction.participants.filter(
            pk=self.stakeholder_id
        ).exists():
            raise ValidationError(
                {"stakeholder": "Attendance must reference an interaction participant."}
            )

    @property
    def display_name(self):
        return self.stakeholder.name if self.stakeholder_id else self.participant_name
