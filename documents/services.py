from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.db.models import Max
from django.utils import timezone

from accounts.access import accessible_queryset, has_access
from accounts.models import RoleGrant
from audit.models import AuditEvent
from documents.models import Document, DocumentVersion


def accessible_documents(user, *, include_archived=False):
    queryset = Document.objects.all()
    if not include_archived:
        queryset = queryset.filter(archived=False)
    general_documents = accessible_queryset(
        user,
        RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
        RoleGrant.Action.READ,
        queryset,
    )
    confidential_documents = accessible_queryset(
        user,
        RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
        RoleGrant.Action.READ,
        queryset.filter(sensitivity=Document.Sensitivity.CONFIDENTIAL),
    )
    ids = set(general_documents.values_list("pk", flat=True))
    ids.update(confidential_documents.values_list("pk", flat=True))
    candidates = queryset.filter(pk__in=ids).select_related("owner", "created_by")
    return [
        document
        for document in candidates
        if has_access(
            user,
            document.permission_resource,
            RoleGrant.Action.READ,
            document,
        )
    ]


def get_accessible_document(user, document_id, action=RoleGrant.Action.READ):
    try:
        document = Document.objects.select_related("owner", "created_by").get(pk=document_id)
    except (Document.DoesNotExist, ValueError):
        raise Document.DoesNotExist from None
    if not has_access(user, document.permission_resource, action, document):
        raise PermissionDenied
    return document


def create_document_version(document, uploaded_file, content_type, actor, change_note=""):
    if document.archived:
        raise PermissionDenied("Archived documents cannot be replaced.")
    with transaction.atomic():
        current_versions = document.versions.select_for_update().filter(is_current=True)
        version_number = (document.versions.aggregate(value=Max("version_number"))["value"] or 0) + 1
        current_versions.update(is_current=False)
        version = DocumentVersion(
            document=document,
            original_filename=uploaded_file.name.rsplit("\\", 1)[-1].rsplit("/", 1)[-1][:255],
            content_type=content_type,
            size_bytes=uploaded_file.size,
            version_number=version_number,
            change_note=change_note,
            uploaded_by=actor,
            metadata_snapshot={
                "title": document.title,
                "category": document.category,
                "reference_number": document.reference_number,
                "issuing_authority": document.issuing_authority,
                "issue_date": document.issue_date.isoformat() if document.issue_date else None,
                "expiry_date": document.expiry_date.isoformat() if document.expiry_date else None,
                "sensitivity": document.sensitivity,
                "owner_id": document.owner_id,
                "notes": document.notes,
                "tags": document.tags,
            },
        )
        version.file.save(uploaded_file.name, uploaded_file, save=False)
        try:
            version.save()
        except DatabaseError:
            version.file.delete(save=False)
            raise
    return version


def archive_document(document, actor):
    document.archived = True
    document.archived_at = timezone.now()
    document.archived_by = actor
    document.save(update_fields=("archived", "archived_at", "archived_by", "updated_at"))


def audit_document_view(request, document):
    from audit.services import log_document_access

    log_document_access(request, document, AuditEvent.Action.DOCUMENT_VIEW)


def document_resource_for_sensitivity(sensitivity):
    if sensitivity == Document.Sensitivity.CONFIDENTIAL:
        return RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS
    return RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS
