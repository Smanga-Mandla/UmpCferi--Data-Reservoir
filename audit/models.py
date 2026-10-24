from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType


class ImmutableAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionDenied("Audit events are immutable.")

    def delete(self):
        raise PermissionDenied("Audit events are immutable.")


class AuditEvent(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", "Created"
        UPDATE = "update", "Updated"
        DELETE = "delete", "Deleted"
        SENSITIVE_VIEW = "sensitive_view", "Sensitive record viewed"
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        PERMISSION_CHANGE = "permission_change", "Permission changed"
        DOCUMENT_VIEW = "document_view", "Document viewed"
        DOCUMENT_DOWNLOAD = "document_download", "Document downloaded"
        EXPORT = "export", "Data exported"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    resource = models.CharField(max_length=64)
    object_id = models.CharField(max_length=64)
    document_uuid = models.UUIDField(null=True, blank=True)
    version_uuid = models.UUIDField(null=True, blank=True)
    content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.PROTECT
    )
    target = GenericForeignKey("content_type", "object_id")
    metadata = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=32, default="success")
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)

    objects = ImmutableAuditQuerySet.as_manager()

    class Meta:
        ordering = ("-occurred_at",)

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise PermissionDenied("Audit events are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Audit events are immutable.")

    def __str__(self):
        return f"{self.get_action_display()}:{self.resource}:{self.object_id}:{self.actor_id}"
