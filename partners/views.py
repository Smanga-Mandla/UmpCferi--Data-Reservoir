from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.access import (
    accessible_queryset,
    has_access,
    require_access,
    soft_delete_record,
)
from accounts.models import RoleGrant
from audit.models import AuditEvent
from audit.services import log_event
from audit.services import log_document_access, log_sensitive_record_view
from partners.forms import (
    MOUAttachmentForm,
    MOUForm,
    MOUObligationForm,
    PartnerForm,
    PartnerRepresentativeForm,
)
from partners.models import (
    MOU,
    MOUAttachment,
    MOUExpiryAlert,
    MOUHistory,
    MOUObligation,
    Partner,
    PartnerImage,
    PartnerRepresentative,
)
from stakeholders.access import can_view_contact_details
from stakeholders.models import Stakeholder, StakeholderTypeAssignment

RESOURCE = RoleGrant.Resource.PARTNERS_MOUS


def _authorized_queryset(user, queryset, action=RoleGrant.Action.READ):
    return accessible_queryset(user, RESOURCE, action, queryset)


def _accessible_mous(user, queryset=None):
    readable_partners = _authorized_queryset(user, Partner.objects.all())
    source = queryset if queryset is not None else MOU.objects.all()
    candidates = _authorized_queryset(
        user,
        source.filter(partner_id__in=readable_partners.values("pk")),
    ).select_related("partner", "owner", "created_by")
    return [
        mou
        for mou in candidates
        if has_access(user, RESOURCE, RoleGrant.Action.READ, mou.partner)
        and has_access(user, RESOURCE, RoleGrant.Action.READ, mou)
    ]


def _get_partner(user, partner_id, action=RoleGrant.Action.READ):
    partner = get_object_or_404(Partner.objects.select_related("owner", "created_by"), pk=partner_id)
    if not has_access(user, RESOURCE, action, partner):
        raise PermissionDenied
    return partner


def _get_mou(user, mou_id, action=RoleGrant.Action.READ):
    mou = get_object_or_404(
        MOU.objects.select_related(
            "partner",
            "owner",
            "created_by",
            "primary_representative",
            "secondary_representative",
            "renewed_from",
        ),
        pk=mou_id,
    )
    if not has_access(user, RESOURCE, action, mou):
        raise PermissionDenied
    if mou.partner.is_deleted or not has_access(
        user,
        RESOURCE,
        RoleGrant.Action.READ,
        mou.partner,
    ):
        raise PermissionDenied
    return mou


def _can_read_stakeholder(user, stakeholder):
    return bool(
        stakeholder
        and has_access(
            user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            stakeholder,
        )
    )


def _visible_obligations(user, mou):
    obligations = [
        obligation
        for obligation in mou.obligations.select_related("owner", "created_by")
        if has_access(user, RESOURCE, RoleGrant.Action.READ, obligation)
    ]
    for obligation in obligations:
        obligation.can_update = has_access(
            user,
            RESOURCE,
            RoleGrant.Action.UPDATE,
            obligation,
        )
        obligation.can_delete = has_access(
            user,
            RESOURCE,
            RoleGrant.Action.DELETE,
            obligation,
        )
    return obligations


def _save_mou_history(mou, actor):
    MOUHistory.objects.create(
        mou=mou,
        snapshot=mou.snapshot(),
        changed_by=actor,
    )


@login_required
def partner_list(request):
    partners = _authorized_queryset(
        request.user,
        Partner.objects.select_related("owner", "created_by").prefetch_related("images"),
    )
    query = request.GET.get("q", "").strip()
    organisation_type = request.GET.get("type", "")
    if query:
        partners = partners.filter(
            Q(name__icontains=query) | Q(registration_number__icontains=query)
        )
    if organisation_type in Partner.OrganisationType.values:
        partners = partners.filter(organisation_type=organisation_type)
    return render(
        request,
        "partners/partner_list.html",
        {
            "partners": partners,
            "query": query,
            "selected_type": organisation_type,
            "organisation_types": Partner.OrganisationType.choices,
            "can_create": has_access(request.user, RESOURCE, RoleGrant.Action.CREATE),
            "can_export": has_access(request.user, RESOURCE, RoleGrant.Action.READ),
        },
    )


@login_required
def partner_create(request):
    require_access(request.user, RESOURCE, RoleGrant.Action.CREATE)
    form = PartnerForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            partner = form.save(commit=False)
            partner.owner = request.user
            partner.created_by = request.user
            require_access(request.user, RESOURCE, RoleGrant.Action.CREATE, partner)
            partner.save()
            uploaded_image = form.cleaned_data.get("image")
            if uploaded_image:
                PartnerImage.objects.create(
                    partner=partner,
                    file=uploaded_image,
                    uploaded_by=request.user,
                )
        messages.success(request, "Partner organisation created.")
        return redirect("partners:partner_detail", partner_id=partner.pk)
    return render(
        request,
        "partners/partner_form.html",
        {"form": form, "title": "Register partner organisation"},
    )


@login_required
def partner_detail(request, partner_id):
    partner = _get_partner(request.user, partner_id)
    if partner.sensitivity in {"confidential", "restricted"}:
        log_sensitive_record_view(request, partner)
    visible_mous = _accessible_mous(
        request.user,
        MOU.objects.filter(partner=partner),
    )
    representative_links = PartnerRepresentative.objects.filter(
        partner=partner,
        is_active=True,
    ).select_related(
        "stakeholder"
    )
    representatives = []
    for link in representative_links:
        if not _can_read_stakeholder(request.user, link.stakeholder):
            continue
        contact = link.stakeholder
        contact.show_contact_details = can_view_contact_details(
            request.user, contact
        )
        contact.can_unlink = not partner.mous.filter(is_deleted=False).filter(
            Q(primary_representative=contact) | Q(secondary_representative=contact)
        ).exists()
        representatives.append(contact)
    return render(
        request,
        "partners/partner_detail.html",
        {
            "partner": partner,
            "mous": visible_mous,
            "representatives": representatives,
            "can_update": has_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, partner),
            "can_create_mou": has_access(request.user, RESOURCE, RoleGrant.Action.CREATE),
            "can_view_contact_details": any(
                contact.show_contact_details for contact in representatives
            ),
        },
    )


@login_required
def partner_image(request, partner_id, image_id):
    partner = _get_partner(request.user, partner_id)
    image = get_object_or_404(PartnerImage, partner=partner, pk=image_id)
    response = FileResponse(
        image.file.open("rb"),
        content_type={
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(Path(image.file.name).suffix.lower(), "application/octet-stream"),
    )
    response["Cache-Control"] = "private, no-store"
    response["Content-Disposition"] = "inline"
    response["X-Content-Type-Options"] = "nosniff"
    log_event(
        request=request,
        action=AuditEvent.Action.DOCUMENT_VIEW,
        target=partner,
        resource="partners.partner_image",
        metadata={"image_id": str(image.pk)},
    )
    return response


@login_required
def partner_update(request, partner_id):
    partner = _get_partner(request.user, partner_id, RoleGrant.Action.UPDATE)
    form = PartnerForm(request.POST or None, request.FILES or None, instance=partner)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            updated_partner = form.save(commit=False)
            require_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, updated_partner)
            updated_partner.save()
            uploaded_image = form.cleaned_data.get("image")
            if uploaded_image:
                PartnerImage.objects.create(
                    partner=updated_partner,
                    file=uploaded_image,
                    uploaded_by=request.user,
                )
        messages.success(request, "Partner organisation updated.")
        return redirect("partners:partner_detail", partner_id=partner.pk)
    return render(
        request,
        "partners/partner_form.html",
        {"form": form, "title": f"Edit {partner.name}", "partner": partner},
    )


@login_required
@require_POST
def partner_delete(request, partner_id):
    partner = _get_partner(request.user, partner_id, RoleGrant.Action.DELETE)
    if partner.mous.exists():
        messages.error(request, "Soft-delete or transfer the partner's active MOUs before deleting this organisation.")
        return redirect("partners:partner_detail", partner_id=partner.pk)
    soft_delete_record(request.user, RESOURCE, partner)
    messages.success(request, "Partner organisation was soft-deleted; its agreement history is retained.")
    return redirect("partners:partner_list")


@login_required
def representative_add(request, partner_id):
    partner = _get_partner(request.user, partner_id, RoleGrant.Action.UPDATE)
    form = PartnerRepresentativeForm(request.user, partner, request.POST or None)
    if request.method == "POST" and form.is_valid():
        stakeholder = form.cleaned_data["stakeholder"]
        if not PartnerRepresentative.objects.filter(
            partner=partner,
            stakeholder=stakeholder,
            is_active=True,
        ).exists():
            PartnerRepresentative.objects.create(
                partner=partner,
                stakeholder=stakeholder,
                linked_by=request.user,
            )
        StakeholderTypeAssignment.objects.get_or_create(
            stakeholder=stakeholder,
            relationship_type=Stakeholder.RelationshipType.PARTNER_REPRESENTATIVE,
            is_active=True,
            defaults={"assigned_by": request.user},
        )
        messages.success(request, "Existing stakeholder linked to the partner.")
        return redirect("partners:partner_detail", partner_id=partner.pk)
    return render(
        request,
        "partners/representative_form.html",
        {"form": form, "partner": partner},
    )


@login_required
@require_POST
def representative_remove(request, partner_id, stakeholder_id):
    partner = _get_partner(request.user, partner_id, RoleGrant.Action.UPDATE)
    stakeholder = get_object_or_404(Stakeholder.objects.all(), pk=stakeholder_id)
    if not has_access(
        request.user,
        RoleGrant.Resource.STAKEHOLDERS,
        RoleGrant.Action.READ,
        stakeholder,
    ):
        raise PermissionDenied
    if partner.mous.filter(is_deleted=False).filter(
        Q(primary_representative=stakeholder) | Q(secondary_representative=stakeholder)
    ).exists():
        raise PermissionDenied("A representative linked to an active MOU cannot be unlinked.")
    PartnerRepresentative.objects.filter(
        partner=partner,
        stakeholder=stakeholder,
        is_active=True,
    ).update(
        is_active=False,
        unlinked_at=timezone.now(),
        unlinked_by=request.user,
    )
    messages.success(request, "Stakeholder unlinked; the contact and link history were retained.")
    return redirect("partners:partner_detail", partner_id=partner.pk)


@login_required
def mou_list(request):
    mous = _accessible_mous(request.user)
    status = request.GET.get("status", "")
    partner_id = request.GET.get("partner", "")
    if status in MOU.Status.values:
        mous = [mou for mou in mous if mou.status == status]
    if partner_id:
        mous = [mou for mou in mous if str(mou.partner_id) == partner_id]
    return render(
        request,
        "partners/mou_list.html",
        {
            "mous": mous,
            "selected_status": status,
            "statuses": MOU.Status.choices,
            "can_create": has_access(request.user, RESOURCE, RoleGrant.Action.CREATE),
            "can_export": has_access(request.user, RESOURCE, RoleGrant.Action.READ),
        },
    )


@login_required
def mou_create(request):
    require_access(request.user, RESOURCE, RoleGrant.Action.CREATE)
    initial_partner = None
    partner_id = request.GET.get("partner")
    if partner_id:
        initial_partner = _get_partner(request.user, partner_id)
    form = MOUForm(
        request.user,
        request.POST or None,
        instance=None,
        initial_partner=initial_partner,
    )
    if request.method == "POST" and form.is_valid():
        require_access(request.user, RESOURCE, RoleGrant.Action.CREATE)
        mou = form.save(commit=False)
        mou.owner = request.user
        mou.created_by = request.user
        require_access(request.user, RESOURCE, RoleGrant.Action.CREATE, mou)
        with transaction.atomic():
            mou.save()
            _save_mou_history(mou, request.user)
        messages.success(request, "MOU created; its private document folder is ready.")
        return redirect("partners:mou_detail", mou_id=mou.pk)
    return render(request, "partners/mou_form.html", {"form": form, "title": "Create MOU"})


@login_required
def mou_detail(request, mou_id):
    mou = _get_mou(request.user, mou_id)
    if mou.sensitivity in {"confidential", "restricted"}:
        log_sensitive_record_view(request, mou)
    attachments = mou.attachments.select_related("uploaded_by")
    for attachment in attachments:
        log_document_access(
            request,
            mou,
            AuditEvent.Action.DOCUMENT_VIEW,
            version=attachment,
            resource="partners.mou_attachment",
        )
    obligations = _visible_obligations(request.user, mou)
    return render(
        request,
        "partners/mou_detail.html",
        {
            "mou": mou,
            "attachments": attachments,
            "attachment_form": MOUAttachmentForm(),
            "obligations": obligations,
            "history": mou.history.select_related("changed_by"),
            "primary_representative_visible": _can_read_stakeholder(
                request.user, mou.primary_representative
            ),
            "secondary_representative_visible": _can_read_stakeholder(
                request.user, mou.secondary_representative
            ),
            "can_view_contact_details": can_view_contact_details(
                request.user, mou.primary_representative
            )
            or can_view_contact_details(request.user, mou.secondary_representative),
            "can_update": has_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, mou),
            "can_upload": has_access(request.user, RESOURCE, RoleGrant.Action.CREATE, mou),
            "can_create_obligation": has_access(
                request.user,
                RESOURCE,
                RoleGrant.Action.CREATE,
                mou,
            ),
            "renewed_from_visible": bool(
                mou.renewed_from
                and has_access(request.user, RESOURCE, RoleGrant.Action.READ, mou.renewed_from)
            ),
        },
    )


@login_required
def mou_update(request, mou_id):
    mou = _get_mou(request.user, mou_id, RoleGrant.Action.UPDATE)
    form = MOUForm(request.user, request.POST or None, instance=mou)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            updated_mou = form.save(commit=False)
            require_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, updated_mou)
            updated_mou.save()
            _save_mou_history(updated_mou, request.user)
        messages.success(request, "MOU updated; a new agreement-history snapshot was recorded.")
        return redirect("partners:mou_detail", mou_id=mou.pk)
    return render(
        request,
        "partners/mou_form.html",
        {"form": form, "title": f"Edit {mou.title}", "mou": mou},
    )


@login_required
@require_POST
def mou_delete(request, mou_id):
    mou = _get_mou(request.user, mou_id, RoleGrant.Action.DELETE)
    soft_delete_record(request.user, RESOURCE, mou)
    messages.success(request, "MOU was soft-deleted; its history and private files are retained.")
    return redirect("partners:mou_list")


@login_required
@require_POST
def mou_attachment_add(request, mou_id):
    mou = _get_mou(request.user, mou_id, RoleGrant.Action.CREATE)
    form = MOUAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        attachment = form.save(commit=False)
        attachment.mou = mou
        attachment.uploaded_by = request.user
        attachment.original_filename = Path(form.cleaned_data["file"].name).name[:255]
        attachment.save()
        messages.success(request, "MOU document uploaded to its private agreement folder.")
        return redirect("partners:mou_detail", mou_id=mou.pk)
    return render(
        request,
        "partners/mou_detail.html",
        {
            "mou": mou,
            "attachments": mou.attachments.all(),
            "attachment_form": form,
            "obligations": _visible_obligations(request.user, mou),
            "history": mou.history.select_related("changed_by"),
            "attachment_form": form,
            "primary_representative_visible": _can_read_stakeholder(
                request.user, mou.primary_representative
            ),
            "secondary_representative_visible": _can_read_stakeholder(
                request.user, mou.secondary_representative
            ),
            "can_view_contact_details": can_view_contact_details(
                request.user, mou.primary_representative
            )
            or can_view_contact_details(request.user, mou.secondary_representative),
            "can_update": has_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, mou),
            "can_upload": True,
            "can_create_obligation": has_access(
                request.user,
                RESOURCE,
                RoleGrant.Action.CREATE,
                mou,
            ),
            "renewed_from_visible": bool(
                mou.renewed_from
                and has_access(request.user, RESOURCE, RoleGrant.Action.READ, mou.renewed_from)
            ),
        },
    )


@login_required
def mou_attachment_download(request, mou_id, attachment_id):
    mou = _get_mou(request.user, mou_id)
    attachment = get_object_or_404(mou.attachments, pk=attachment_id)
    log_document_access(
        request,
        mou,
        AuditEvent.Action.DOCUMENT_DOWNLOAD,
        version=attachment,
        resource="partners.mou_attachment",
    )
    response = FileResponse(
        attachment.file.open("rb"),
        as_attachment=True,
        filename=attachment.original_filename,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def mou_obligation_form(request, mou_id, obligation_id=None):
    mou = _get_mou(request.user, mou_id)
    obligation = None
    if obligation_id is not None:
        obligation = get_object_or_404(mou.obligations, pk=obligation_id)
        if not has_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, obligation):
            raise PermissionDenied
        action = RoleGrant.Action.UPDATE
    else:
        action = RoleGrant.Action.CREATE
    require_access(request.user, RESOURCE, action, mou)
    form = MOUObligationForm(request.POST or None, instance=obligation)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            item = form.save(commit=False)
            item.mou = mou
            if obligation is None:
                item.owner = form.cleaned_data["owner"]
                item.created_by = request.user
            item.save()
        messages.success(request, "MOU obligation saved.")
        return redirect("partners:mou_detail", mou_id=mou.pk)
    return render(
        request,
        "partners/obligation_form.html",
        {"form": form, "mou": mou, "obligation": obligation},
    )


@login_required
@require_POST
def mou_obligation_delete(request, mou_id, obligation_id):
    mou = _get_mou(request.user, mou_id)
    obligation = get_object_or_404(mou.obligations, pk=obligation_id)
    if not has_access(request.user, RESOURCE, RoleGrant.Action.DELETE, obligation):
        raise PermissionDenied
    soft_delete_record(request.user, RESOURCE, obligation)
    messages.success(request, "Obligation soft-deleted.")
    return redirect("partners:mou_detail", mou_id=mou.pk)


@login_required
def mou_history(request, mou_id):
    mou = _get_mou(request.user, mou_id)
    return render(
        request,
        "partners/mou_history.html",
        {"mou": mou, "history": mou.history.select_related("changed_by")},
    )


@login_required
def mou_expiry_alerts(request):
    accessible_mous = _accessible_mous(request.user)
    alerts = MOUExpiryAlert.objects.filter(
        mou_id__in=[mou.pk for mou in accessible_mous],
        mou__status=MOU.Status.ACTIVE,
        end_date=F("mou__end_date"),
    ).select_related("mou", "mou__partner")
    return render(
        request,
        "partners/mou_expiry_alerts.html",
        {
            "alerts": alerts,
            "can_export": has_access(
                request.user, RESOURCE, RoleGrant.Action.READ
            ),
        },
    )
