from contextvars import ContextVar
from uuid import UUID

from django.contrib.contenttypes.models import ContentType

from audit.models import AuditEvent

_current_request = ContextVar("audit_current_request", default=None)


def set_current_request(request):
    return _current_request.set(request)


def reset_current_request(token):
    _current_request.reset(token)


def current_actor():
    request = _current_request.get()
    return request.user if request and request.user.is_authenticated else None


def log_event(
    *,
    actor=None,
    action,
    target=None,
    resource=None,
    object_id=None,
    metadata=None,
    outcome="success",
    request=None,
    version_uuid=None,
):
    request = request or _current_request.get()
    actor = actor if actor is not None else (
        request.user if request and request.user.is_authenticated else None
    )
    content_type = (
        ContentType.objects.get_for_model(target, for_concrete_model=False)
        if target is not None
        else None
    )
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        resource=resource
        or (content_type.model if content_type is not None else "system"),
        object_id=str(object_id if object_id is not None else getattr(target, "pk", "")),
        document_uuid=(
            getattr(target, "pk", None)
            if isinstance(getattr(target, "pk", None), UUID)
            and action
            in {AuditEvent.Action.DOCUMENT_VIEW, AuditEvent.Action.DOCUMENT_DOWNLOAD}
            and (
                getattr(getattr(target, "_meta", None), "model_name", None)
                == "document"
                or resource == "partners.mou_attachment"
            )
            else None
        ),
        version_uuid=version_uuid,
        content_type=content_type,
        metadata=metadata or {},
        outcome=outcome,
        ip_address=request.META.get("REMOTE_ADDR") if request else None,
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:512] if request else "",
    )


def log_document_access(
    request,
    document,
    action,
    version=None,
    resource="documents.centre_document",
):
    actor = request.user if request.user.is_authenticated else None
    return log_event(
        actor=actor,
        action=action,
        target=document,
        resource=resource,
        metadata={"version_id": str(version.pk)} if version else {},
        request=request,
        version_uuid=version.pk if version else None,
    )


def log_sensitive_record_view(request, target, *, metadata=None):
    return log_event(
        actor=request.user if request.user.is_authenticated else None,
        action=AuditEvent.Action.SENSITIVE_VIEW,
        target=target,
        metadata=metadata or {},
        request=request,
    )
