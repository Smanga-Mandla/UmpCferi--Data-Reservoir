from datetime import date
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.db.models import Max
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.access import accessible_queryset
from accounts.models import Role, RoleGrant
from audit.models import AuditEvent
from audit.services import log_event
from incubation.account_access import can_manage_incubatee_accounts, manageable_businesses
from incubation.models import (
    BusinessDocument,
    BusinessDocumentVersion,
    Incubatee,
    IncubateeAccount,
    IncubateePortalNotification,
    IncubateeProfileChange,
)
from incubation.portal_forms import BusinessDocumentForm, BusinessProfileForm, ProtectedProfileChangeForm
from incubation.portal_notifications import notify_business_accounts

PROFILE_EDIT_FIELDS = (
    "trading_name",
    "sic_code",
    "sub_sector",
    "products_services",
    "business_model",
    "business_stage",
    "physical_address",
    "province",
    "municipality",
    "business_contact_email",
    "business_contact_number",
)
PROTECTED_PROFILE_FIELDS = (
    "legal_name",
    "cipc_registration_number",
    "entity_type",
    "date_registered",
)


def _business_for_portal(user, incubatee_id):
    if user.roles.filter(code=Role.Code.INCUBATEE).exists():
        account = get_object_or_404(IncubateeAccount, user=user)
        businesses = accessible_queryset(
            user,
            RoleGrant.Resource.INCUBATEES,
            RoleGrant.Action.READ,
            Incubatee.objects.all(),
        ).exclude(sensitivity__in=("confidential", "restricted"))
        return get_object_or_404(
            businesses.filter(account_links__account=account).distinct(),
            pk=incubatee_id,
        )
    if can_manage_incubatee_accounts(user):
        return get_object_or_404(manageable_businesses(user), pk=incubatee_id)
    raise PermissionDenied


def _require_incubatee(user):
    if not user.roles.filter(code=Role.Code.INCUBATEE).exists():
        raise PermissionDenied


def _require_profile_reviewer(user):
    if not can_manage_incubatee_accounts(user):
        raise PermissionDenied


def _profile_context(request, business, profile_form=None, protected_form=None):
    return {
        "business": business,
        "profile_form": profile_form or BusinessProfileForm(instance=business),
        "protected_form": protected_form or ProtectedProfileChangeForm(incubatee=business),
        "change_requests": business.profile_change_requests.filter(
            requested_by=request.user
        ),
    }


@login_required
def business_profile(request, incubatee_id):
    business = _business_for_portal(request.user, incubatee_id)
    is_incubatee = request.user.roles.filter(code=Role.Code.INCUBATEE).exists()
    if request.method == "POST":
        if not is_incubatee:
            raise PermissionDenied
        form_type = request.POST.get("form_type")
        if form_type == "approved_fields":
            old_values = {
                field: getattr(business, field)
                for field in PROFILE_EDIT_FIELDS
            }
            form = BusinessProfileForm(request.POST, instance=business)
            if form.is_valid():
                changed_fields = [
                    field
                    for field in PROFILE_EDIT_FIELDS
                    if form.cleaned_data[field] != old_values[field]
                ]
                if changed_fields:
                    with transaction.atomic():
                        updated = form.save(commit=False)
                        updated.updated_by = request.user
                        updated.save(update_fields=(*changed_fields, "updated_by", "updated_at"))
                        log_event(
                            actor=request.user,
                            action=AuditEvent.Action.UPDATE,
                            target=updated,
                            resource="incubation.business_profile",
                            metadata={
                                "business_id": str(business.pk),
                                "changed_fields": {
                                    field: {
                                        "old": old_values[field],
                                        "new": form.cleaned_data[field],
                                    }
                                    for field in changed_fields
                                },
                            },
                            request=request,
                        )
                    messages.success(request, "Your approved business profile fields were updated.")
                else:
                    messages.info(request, "No business profile changes were submitted.")
                return redirect("incubation:business_profile", incubatee_id=business.pk)
            return render(
                request,
                "incubation/portal/business_profile.html",
                {
                    **_profile_context(request, business, profile_form=form),
                    "is_incubatee": True,
                },
            )
        if form_type == "protected_fields":
            form = ProtectedProfileChangeForm(request.POST, incubatee=business)
            if form.is_valid():
                proposed_values = form.proposed_values()
                if not proposed_values:
                    messages.info(request, "No protected-field changes were submitted.")
                    return redirect("incubation:business_profile", incubatee_id=business.pk)
                pending_changes = business.profile_change_requests.filter(
                    status=IncubateeProfileChange.Status.PENDING
                )
                already_pending = {
                    field
                    for change in pending_changes
                    for field in change.proposed_values
                }
                if already_pending.intersection(proposed_values):
                    form.add_error(
                        None,
                        "A pending request already exists for one or more of these fields. Wait for CFERI review.",
                    )
                else:
                    old_values = {
                        field: (
                            getattr(business, field).isoformat()
                            if isinstance(getattr(business, field), date)
                            else getattr(business, field)
                        )
                        for field in proposed_values
                    }
                    with transaction.atomic():
                        change = IncubateeProfileChange.objects.create(
                            incubatee=business,
                            requested_by=request.user,
                            request_note=form.cleaned_data["reason"],
                            old_values=old_values,
                            proposed_values=proposed_values,
                        )
                        log_event(
                            actor=request.user,
                            action=AuditEvent.Action.CREATE,
                            target=change,
                            resource="incubation.profile_change_request",
                            metadata={
                                "business_id": str(business.pk),
                                "fields": list(proposed_values),
                            },
                            request=request,
                        )
                    messages.success(request, "Your protected-field changes were sent to CFERI for review.")
                    return redirect("incubation:business_profile", incubatee_id=business.pk)
            return render(
                request,
                "incubation/portal/business_profile.html",
                {
                    **_profile_context(request, business, protected_form=form),
                    "is_incubatee": True,
                },
            )
        raise PermissionDenied

    return render(
        request,
        "incubation/portal/business_profile.html",
        {
            **_profile_context(request, business),
            "is_incubatee": is_incubatee,
        },
    )


@login_required
def profile_change_review_list(request):
    _require_profile_reviewer(request.user)
    businesses = manageable_businesses(request.user)
    changes = IncubateeProfileChange.objects.filter(
        incubatee__in=businesses
    ).select_related("incubatee", "requested_by", "reviewer")
    return render(
        request,
        "incubation/portal/profile_change_review_list.html",
        {"changes": changes},
    )


@login_required
@require_POST
def profile_change_review(request, change_id):
    _require_profile_reviewer(request.user)
    change = get_object_or_404(
        IncubateeProfileChange.objects.select_related("incubatee"),
        pk=change_id,
        incubatee__in=manageable_businesses(request.user),
    )
    if change.status != IncubateeProfileChange.Status.PENDING:
        raise PermissionDenied("This profile change request has already been reviewed.")
    decision = request.POST.get("decision")
    if decision not in {"approve", "reject"}:
        raise PermissionDenied("Choose a valid review decision.")
    note = request.POST.get("review_note", "").strip()[:2000]

    with transaction.atomic():
        change = IncubateeProfileChange.objects.select_for_update().get(pk=change.pk)
        business = Incubatee.objects.select_for_update().get(pk=change.incubatee_id)
        if change.status != IncubateeProfileChange.Status.PENDING:
            raise PermissionDenied("This profile change request has already been reviewed.")
        if decision == "approve":
            stale_fields = []
            for field, old_value in change.old_values.items():
                current_value = getattr(business, field)
                if isinstance(current_value, date):
                    current_value = current_value.isoformat()
                if current_value != old_value:
                    stale_fields.append(field)
            if stale_fields:
                change.status = IncubateeProfileChange.Status.STALE
                change.review_note = (
                    f"Current business information changed for: {', '.join(stale_fields)}. "
                    "The incubatee must submit a new proposal."
                )
                change.reviewer = request.user
                change.reviewed_at = timezone.now()
                change.save(
                    update_fields=("status", "review_note", "reviewer", "reviewed_at")
                )
                notify_business_accounts(
                    business=business,
                    notification_type=IncubateePortalNotification.Type.PROFILE_CHANGE,
                    title="Business profile change needs resubmission",
                    message="CFERI could not apply your proposed business profile change because the recorded information changed. Please review the decision and submit a new proposal if needed.",
                )
                log_event(
                    actor=request.user,
                    action=AuditEvent.Action.UPDATE,
                    target=change,
                    resource="incubation.profile_change_request",
                    metadata={"decision": "stale", "fields": stale_fields},
                    request=request,
                )
                messages.warning(
                    request,
                    "The proposal was not applied because the business information changed since it was submitted.",
                )
                return redirect("incubation:profile_change_review_list")
            old_values = {
                field: getattr(business, field)
                for field in change.proposed_values
            }
            for field, value in change.proposed_values.items():
                if field == "date_registered" and value:
                    value = date.fromisoformat(value)
                setattr(business, field, value)
            business.updated_by = request.user
            business.full_clean()
            business.save(
                update_fields=(*change.proposed_values.keys(), "updated_by", "updated_at")
            )
            change.status = IncubateeProfileChange.Status.APPROVED
            action_name = "approved"
        else:
            change.status = IncubateeProfileChange.Status.REJECTED
            action_name = "rejected"
        change.reviewer = request.user
        change.review_note = note
        change.reviewed_at = timezone.now()
        change.save(update_fields=("status", "reviewer", "review_note", "reviewed_at"))
        notify_business_accounts(
            business=business,
            notification_type=IncubateePortalNotification.Type.PROFILE_CHANGE,
            title=f"Business profile change {action_name}",
            message=(
                "CFERI has approved your proposed business profile change."
                if decision == "approve"
                else "CFERI has rejected your proposed business profile change."
            ),
        )
        log_event(
            actor=request.user,
            action=AuditEvent.Action.UPDATE,
            target=change,
            resource="incubation.profile_change_request",
            metadata={
                "decision": action_name,
                "business_id": str(business.pk),
                "fields": list(change.proposed_values),
                "old_values": {
                    field: value.isoformat() if isinstance(value, date) else value
                    for field, value in old_values.items()
                }
                if decision == "approve"
                else {},
                "new_values": change.proposed_values if decision == "approve" else {},
                "review_note": note,
            },
            request=request,
        )
    messages.success(request, f"Profile change request {action_name}.")
    return redirect("incubation:profile_change_review_list")


def _document_metadata(document):
    return {
        "title": document.title,
        "category": document.category,
        "description": document.description,
        "expiry_date": document.expiry_date.isoformat() if document.expiry_date else None,
        "status": document.status,
    }


def _save_document_version(document, uploaded_file, content_type, actor, change_note):
    with transaction.atomic():
        document = BusinessDocument.objects.select_for_update().get(pk=document.pk)
        version_number = (
            document.versions.aggregate(value=Max("version_number"))["value"] or 0
        ) + 1
        document.versions.filter(is_current=True).update(is_current=False)
        version = BusinessDocumentVersion(
            document=document,
            original_filename=Path(uploaded_file.name.replace("\\", "/")).name[:255],
            content_type=content_type,
            size_bytes=uploaded_file.size,
            version_number=version_number,
            metadata_snapshot=_document_metadata(document),
            change_note=change_note,
            uploaded_by=actor,
        )
        version.file.save(uploaded_file.name, uploaded_file, save=False)
        try:
            version.save()
        except DatabaseError:
            version.file.delete(save=False)
            raise
    return version


@login_required
def business_document_list(request, incubatee_id):
    business = _business_for_portal(request.user, incubatee_id)
    documents = BusinessDocument.objects.filter(incubatee=business).prefetch_related(
        "versions"
    )
    return render(
        request,
        "incubation/portal/business_document_list.html",
        {
            "business": business,
            "documents": documents,
            "can_upload": request.user.roles.filter(code=Role.Code.INCUBATEE).exists(),
            "is_reviewer": can_manage_incubatee_accounts(request.user),
        },
    )


@login_required
def business_document_upload(request, incubatee_id, document_id=None):
    _require_incubatee(request.user)
    business = _business_for_portal(request.user, incubatee_id)
    document = None
    if document_id:
        document = get_object_or_404(
            BusinessDocument, pk=document_id, incubatee=business
        )
    form = BusinessDocumentForm(
        request.POST or None,
        request.FILES or None,
        instance=document,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        uploaded_file = data["file"]
        new_document = None
        version = None
        try:
            with transaction.atomic():
                if document is None:
                    new_document = BusinessDocument(
                        incubatee=business,
                        uploaded_by=request.user,
                        **form.metadata(),
                    )
                    new_document.save()
                    target_document = new_document
                else:
                    target_document = BusinessDocument.objects.select_for_update().get(
                        pk=document.pk
                    )
                    for field, value in form.metadata().items():
                        setattr(target_document, field, value)
                    target_document.status = BusinessDocument.Status.PENDING
                    target_document.reviewed_by = None
                    target_document.review_note = ""
                    target_document.reviewed_at = None
                    target_document.updated_at = timezone.now()
                    target_document.save(
                        update_fields=(
                            *form.metadata().keys(),
                            "status",
                            "reviewed_by",
                            "review_note",
                            "reviewed_at",
                            "updated_at",
                        )
                    )
                version = _save_document_version(
                    target_document,
                    uploaded_file,
                    data["detected_content_type"],
                    request.user,
                    data["change_note"],
                )
                log_event(
                    actor=request.user,
                    action=AuditEvent.Action.CREATE,
                    target=target_document,
                    resource="incubation.business_document",
                    metadata={
                        "business_id": str(business.pk),
                        "version_number": version.version_number,
                        "category": target_document.category,
                    },
                    request=request,
                    version_uuid=version.pk,
                )
        except DatabaseError:
            if version is not None and version.file.name:
                version.file.delete(save=False)
            raise
        messages.success(
            request,
            "Business document uploaded and sent to CFERI for review."
            if document is None
            else "A new document version was uploaded and sent to CFERI for review.",
        )
        return redirect(
            "incubation:business_document_list",
            incubatee_id=business.pk,
        )
    return render(
        request,
        "incubation/portal/business_document_form.html",
        {
            "business": business,
            "document": document,
            "form": form,
            "title": "Upload business document" if document is None else "Upload new document version",
        },
    )


@login_required
def business_document_detail(request, incubatee_id, document_id):
    business = _business_for_portal(request.user, incubatee_id)
    document = get_object_or_404(
        BusinessDocument.objects.select_related("uploaded_by", "reviewed_by").prefetch_related(
            "versions__uploaded_by"
        ),
        pk=document_id,
        incubatee=business,
    )
    log_event(
        actor=request.user,
        action=AuditEvent.Action.DOCUMENT_VIEW,
        target=document,
        resource="incubation.business_document",
        request=request,
    )
    return render(
        request,
        "incubation/portal/business_document_detail.html",
        {
            "business": business,
            "document": document,
            "can_upload": request.user.roles.filter(code=Role.Code.INCUBATEE).exists(),
            "is_reviewer": can_manage_incubatee_accounts(request.user),
        },
    )


@login_required
def business_document_download(request, incubatee_id, document_id, version_id=None):
    business = _business_for_portal(request.user, incubatee_id)
    document = get_object_or_404(
        BusinessDocument, pk=document_id, incubatee=business
    )
    if version_id:
        version = get_object_or_404(
            BusinessDocumentVersion, pk=version_id, document=document
        )
    else:
        version = get_object_or_404(
            BusinessDocumentVersion, document=document, is_current=True
        )
    log_event(
        actor=request.user,
        action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
        target=document,
        resource="incubation.business_document",
        metadata={"version_number": version.version_number},
        request=request,
        version_uuid=version.pk,
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
def business_document_review_list(request):
    _require_profile_reviewer(request.user)
    documents = BusinessDocument.objects.filter(
        incubatee__in=manageable_businesses(request.user),
        status=BusinessDocument.Status.PENDING,
    ).select_related("incubatee", "uploaded_by").prefetch_related("versions")
    return render(
        request,
        "incubation/portal/business_document_review_list.html",
        {"documents": documents},
    )


@login_required
@require_POST
def business_document_review(request, incubatee_id, document_id):
    _require_profile_reviewer(request.user)
    business = get_object_or_404(
        manageable_businesses(request.user), pk=incubatee_id
    )
    with transaction.atomic():
        document = get_object_or_404(
            BusinessDocument.objects.select_for_update(),
            pk=document_id,
            incubatee=business,
        )
        if document.status != BusinessDocument.Status.PENDING:
            raise PermissionDenied("This document has already been reviewed.")
        decision = request.POST.get("decision")
        if decision not in {"approve", "reject"}:
            raise PermissionDenied("Choose a valid document review decision.")
        note = request.POST.get("review_note", "").strip()[:2000]
        document.status = (
            BusinessDocument.Status.APPROVED
            if decision == "approve"
            else BusinessDocument.Status.REJECTED
        )
        document.reviewed_by = request.user
        document.review_note = note
        document.reviewed_at = timezone.now()
        document.save(
            update_fields=("status", "reviewed_by", "review_note", "reviewed_at", "updated_at")
        )
        log_event(
            actor=request.user,
            action=AuditEvent.Action.UPDATE,
            target=document,
            resource="incubation.business_document",
            metadata={
                "decision": document.status,
                "business_id": str(business.pk),
                "version_id": str(document.current_version.pk)
                if document.current_version
                else "",
                "review_note": note,
            },
            request=request,
        )
    messages.success(request, f"Business document {document.get_status_display().lower()}.")
    return redirect("incubation:business_document_review_list")
