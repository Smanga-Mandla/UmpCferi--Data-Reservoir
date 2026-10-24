from django.apps import apps as django_apps
from django.contrib.auth import user_logged_in, user_logged_out, user_login_failed
from django.contrib.auth import get_user_model
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver

from accounts.models import RecordShare, Role, RoleGrant
from audit.models import AuditEvent
from audit.services import current_actor, log_event

TRACKED_APPS = {
    "accounts",
    "documents",
    "partners",
    "stakeholders",
    "incubation",
    "interactions",
}
SENSITIVE_FIELD_NAMES = {
    "password",
    "secret",
    "token",
    "id_number",
    "tax_number",
    "bank_statement",
    "management_accounts",
    "file",
}


def _actor(instance):
    actor = current_actor()
    if actor is not None:
        return actor
    for field_name in ("updated_by", "created_by", "changed_by", "uploaded_by", "recorded_by"):
        candidate = getattr(instance, field_name, None)
        if candidate is not None and getattr(candidate, "is_authenticated", False):
            return candidate
    return None


def _tracked(instance):
    return (
        instance._meta.apps is django_apps
        and instance._meta.app_label in TRACKED_APPS
        and instance._meta.app_label != "audit"
        and instance.__class__ is not AuditEvent
        and instance._meta.model_name not in {"loginthrottle", "totpdevice"}
    )


@receiver(pre_save)
def capture_prior_fields(sender, instance, raw=False, **kwargs):
    if raw or not _tracked(instance) or not instance.pk:
        return
    try:
        previous = sender._base_manager.get(pk=instance.pk)
    except sender.DoesNotExist:
        instance._audit_changed_fields = []
        return
    instance._audit_changed_fields = [
        field.name
        for field in sender._meta.concrete_fields
        if field.name not in SENSITIVE_FIELD_NAMES
        and getattr(previous, field.attname) != getattr(instance, field.attname)
    ]


@receiver(post_save)
def record_model_save(sender, instance, created, raw=False, **kwargs):
    if raw or not _tracked(instance):
        return
    if isinstance(instance, (Role, RoleGrant, RecordShare)):
        action = AuditEvent.Action.PERMISSION_CHANGE
    else:
        action = AuditEvent.Action.CREATE if created else AuditEvent.Action.UPDATE
    log_event(
        actor=_actor(instance),
        action=action,
        target=instance,
        metadata={
            "changed_fields": getattr(instance, "_audit_changed_fields", [])
            if not created
            else [],
        },
    )


@receiver(post_delete)
def record_model_delete(sender, instance, **kwargs):
    if not _tracked(instance):
        return
    action = (
        AuditEvent.Action.PERMISSION_CHANGE
        if isinstance(instance, (Role, RoleGrant, RecordShare))
        else AuditEvent.Action.DELETE
    )
    log_event(
        actor=_actor(instance),
        action=action,
        target=instance,
        metadata={"deleted": True},
    )


@receiver(m2m_changed, sender=get_user_model().roles.through)
def record_user_role_change(sender, instance, action, pk_set, **kwargs):
    if action not in {"post_add", "post_remove", "post_clear"}:
        return
    log_event(
        actor=current_actor(),
        action=AuditEvent.Action.PERMISSION_CHANGE,
        target=instance,
        metadata={
            "relation": "roles",
            "role_ids": sorted(pk_set or []),
            "operation": action,
        },
    )


@receiver(user_logged_in)
def record_successful_login(sender, request, user, **kwargs):
    log_event(
        actor=user,
        action=AuditEvent.Action.LOGIN,
        target=user,
        request=request,
    )


@receiver(user_logged_out)
def record_logout(sender, request, user, **kwargs):
    log_event(
        actor=user,
        action=AuditEvent.Action.LOGOUT,
        target=user,
        request=request,
    )


@receiver(user_login_failed)
def record_failed_login(sender, credentials, request, **kwargs):
    log_event(
        action=AuditEvent.Action.LOGIN,
        resource="accounts.user",
        object_id="",
        metadata={"outcome": "failed"},
        outcome="failure",
        request=request,
    )
