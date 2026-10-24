import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.db.models import F
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.access import has_access, require_access
from accounts.models import RoleGrant
from audit.models import AuditEvent
from audit.services import log_document_access
from documents.forms import DocumentForm
from documents.models import Document, ExpiryAlert
from documents.services import (
    accessible_documents,
    archive_document,
    create_document_version,
    document_resource_for_sensitivity,
    get_accessible_document,
)

logger = logging.getLogger(__name__)


def _visible_document_or_404(user, document_id, action=RoleGrant.Action.READ):
    try:
        return get_accessible_document(user, document_id, action)
    except Document.DoesNotExist:
        raise Http404 from None


def _audit_list_view(request, documents):
    AuditEvent.objects.bulk_create(
        [
            AuditEvent(
                actor=request.user,
                action=AuditEvent.Action.DOCUMENT_VIEW,
                resource="documents.centre_document",
                object_id=str(document.pk),
                document_uuid=document.pk,
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
            )
            for document in documents
        ]
    )


def _form_initial(document):
    return {
        "title": document.title,
        "category": document.category,
        "reference_number": document.reference_number,
        "issuing_authority": document.issuing_authority,
        "issue_date": document.issue_date,
        "expiry_date": document.expiry_date,
        "sensitivity": document.sensitivity,
        "owner": document.owner_id,
        "notes": document.notes,
        "tags": ", ".join(document.tags),
    }


def _save_metadata(document, form):
    metadata = form.metadata()
    for field, value in metadata.items():
        setattr(document, field, value)


@login_required
def document_list(request):
    include_archived = request.GET.get("archived") == "1"
    documents = accessible_documents(request.user, include_archived=include_archived)
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    status = request.GET.get("status", "")
    if query:
        lowered = query.casefold()
        documents = [
            document
            for document in documents
            if any(
                lowered in value.casefold()
                for value in (
                    document.title,
                    document.reference_number,
                    document.issuing_authority,
                    document.notes,
                    " ".join(document.tags),
                    document.get_category_display(),
                    document.issue_date.isoformat() if document.issue_date else "",
                    document.expiry_date.isoformat() if document.expiry_date else "",
                    document.get_sensitivity_display(),
                    document.owner.get_full_name(),
                    document.owner.email,
                )
            )
        ]
    if category:
        documents = [document for document in documents if document.category == category]
    if status:
        documents = [document for document in documents if document.expiry_status == status]

    _audit_list_view(request, documents)
    return render(
        request,
        "documents/document_list.html",
        {
            "documents": documents,
            "categories": Document.Category.choices,
            "query": query,
            "selected_category": category,
            "selected_status": status,
            "include_archived": include_archived,
            "can_export": has_access(
                request.user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
            or has_access(
                request.user,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.READ,
            ),
            "can_create_general": has_access(
                request.user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.CREATE,
            )
            or has_access(
                request.user,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.CREATE,
            ),
        },
    )


@login_required
def document_create(request):
    form = DocumentForm(
        request.POST or None,
        request.FILES or None,
        initial_owner=request.user,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        resource = document_resource_for_sensitivity(data["sensitivity"])
        require_access(request.user, resource, RoleGrant.Action.CREATE)
        if (
            data["sensitivity"] == Document.Sensitivity.RESTRICTED
            and not request.user.roles.filter(code="super_admin").exists()
            and not request.user.is_superuser
        ):
            raise PermissionDenied
        version = None
        try:
            with transaction.atomic():
                document = Document(
                    **form.metadata(),
                    created_by=request.user,
                    is_deleted=False,
                )
                document.save()
                version = create_document_version(
                    document,
                    data["file"],
                    data["detected_content_type"],
                    request.user,
                    data["change_note"],
                )
        except DatabaseError:
            if version is not None and version.file.name:
                version.file.delete(save=False)
            raise
        messages.success(request, "Document uploaded.")
        return redirect("documents:detail", document_id=document.pk)
    return render(
        request,
        "documents/document_form.html",
        {"form": form, "title": "Upload centre document"},
    )


@login_required
def document_detail(request, document_id):
    document = _visible_document_or_404(request.user, document_id)
    log_document_access(request, document, AuditEvent.Action.DOCUMENT_VIEW)
    current_version = document.current_version
    return render(
        request,
        "documents/document_detail.html",
        {
            "document": document,
            "current_version": current_version,
            "can_update": has_access(
                request.user,
                document.permission_resource,
                RoleGrant.Action.UPDATE,
                document,
            ),
        },
    )


@login_required
def document_replace(request, document_id):
    document = _visible_document_or_404(
        request.user,
        document_id,
        RoleGrant.Action.UPDATE,
    )
    if document.archived:
        raise PermissionDenied("Archived documents cannot be replaced.")
    form = DocumentForm(
        request.POST or None,
        request.FILES or None,
        initial=_form_initial(document),
        initial_owner=request.user,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        updated_resource = document_resource_for_sensitivity(data["sensitivity"])
        require_access(request.user, updated_resource, RoleGrant.Action.UPDATE, document)
        _save_metadata(document, form)
        version = None
        try:
            with transaction.atomic():
                document.save()
                version = create_document_version(
                    document,
                    data["file"],
                    data["detected_content_type"],
                    request.user,
                    data["change_note"],
                )
        except DatabaseError:
            if version is not None and version.file.name:
                version.file.delete(save=False)
            raise
        messages.success(request, "A new document version was uploaded.")
        return redirect("documents:detail", document_id=document.pk)
    return render(
        request,
        "documents/document_form.html",
        {"form": form, "title": f"Replace {document.title}", "document": document},
    )


@login_required
def document_versions(request, document_id):
    document = _visible_document_or_404(request.user, document_id)
    versions = list(document.versions.select_related("uploaded_by"))
    log_document_access(request, document, AuditEvent.Action.DOCUMENT_VIEW)
    return render(
        request,
        "documents/document_versions.html",
        {"document": document, "versions": versions},
    )


@login_required
def document_download(request, document_id, version_id=None):
    document = _visible_document_or_404(request.user, document_id)
    if version_id:
        version = get_object_or_404(document.versions, pk=version_id)
    else:
        version = document.current_version
        if version is None:
            raise Http404

    log_document_access(
        request,
        document,
        AuditEvent.Action.DOCUMENT_DOWNLOAD,
        version=version,
    )
    response = FileResponse(
        version.file.open("rb"),
        as_attachment=True,
        filename=version.original_filename,
        content_type=version.content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def document_archive(request, document_id):
    document = _visible_document_or_404(
        request.user,
        document_id,
        RoleGrant.Action.UPDATE,
    )
    if not document.archived:
        archive_document(document, request.user)
        messages.success(request, "Document archived.")
    return redirect("documents:detail", document_id=document.pk)


@login_required
def compliance_status(request):
    documents = accessible_documents(request.user)
    today = timezone.localdate()
    status_counts = {
        "valid": sum(document.expiry_status == "valid" for document in documents),
        "expiring": sum(document.expiry_status == "expiring" for document in documents),
        "expired": sum(document.expiry_status == "expired" for document in documents),
        "no_expiry": sum(document.expiry_status == "no_expiry" for document in documents),
    }
    _audit_list_view(request, documents)
    return render(
        request,
        "documents/compliance_status.html",
        {
            "documents": documents,
            "status_counts": status_counts,
            "today": today,
            "missing_categories_configured": False,
            "can_export": has_access(
                request.user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
            or has_access(
                request.user,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.READ,
            ),
        },
    )


@login_required
def expiry_alert_list(request):
    accessible_ids = [document.pk for document in accessible_documents(request.user)]
    alerts = ExpiryAlert.objects.filter(
        document_id__in=accessible_ids,
        document__archived=False,
        expiry_date=F("document__expiry_date"),
    ).select_related("document").order_by("due_date")
    for alert in alerts:
        log_document_access(request, alert.document, AuditEvent.Action.DOCUMENT_VIEW)
    return render(
        request,
        "documents/expiry_alerts.html",
        {
            "alerts": alerts,
            "can_export": has_access(
                request.user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
            or has_access(
                request.user,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.READ,
            ),
        },
    )
