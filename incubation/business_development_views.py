from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Role
from audit.models import AuditEvent
from audit.services import log_event
from incubation.account_access import can_manage_incubatee_accounts, manageable_businesses
from incubation.business_development_forms import (
    BusinessDevelopmentNeedUpdateForm,
    BusinessDevelopmentNeedsForm,
)
from incubation.models import (
    IncubateeBusinessDevelopmentNeed,
    IncubateeBusinessDevelopmentNeedUpdate,
    IncubateeSupportRequest,
)
from incubation.portal_views import _business_for_portal, _require_incubatee


SUPPORT_REQUEST_CATEGORY_MAP = {
    IncubateeBusinessDevelopmentNeed.Category.BUSINESS_IDEA: IncubateeSupportRequest.Category.OTHER,
    IncubateeBusinessDevelopmentNeed.Category.REFINE_IDEA: IncubateeSupportRequest.Category.OTHER,
    IncubateeBusinessDevelopmentNeed.Category.REGISTRATION: IncubateeSupportRequest.Category.REGISTRATION_COMPLIANCE,
    IncubateeBusinessDevelopmentNeed.Category.MARKETING: IncubateeSupportRequest.Category.MARKETING,
    IncubateeBusinessDevelopmentNeed.Category.PRODUCT_DEVELOPMENT: IncubateeSupportRequest.Category.OTHER,
    IncubateeBusinessDevelopmentNeed.Category.FUNDING: IncubateeSupportRequest.Category.FUNDING_GUIDANCE,
    IncubateeBusinessDevelopmentNeed.Category.FINANCIAL_MANAGEMENT: IncubateeSupportRequest.Category.FINANCIAL_MANAGEMENT,
    IncubateeBusinessDevelopmentNeed.Category.TECHNOLOGY: IncubateeSupportRequest.Category.TECHNOLOGY,
    IncubateeBusinessDevelopmentNeed.Category.MENTORSHIP: IncubateeSupportRequest.Category.MENTORSHIP,
    IncubateeBusinessDevelopmentNeed.Category.MARKET_ACCESS: IncubateeSupportRequest.Category.MARKETING,
    IncubateeBusinessDevelopmentNeed.Category.COMPLIANCE: IncubateeSupportRequest.Category.REGISTRATION_COMPLIANCE,
    IncubateeBusinessDevelopmentNeed.Category.OTHER: IncubateeSupportRequest.Category.OTHER,
}


def _need_description(category, description):
    category_label = IncubateeBusinessDevelopmentNeed.Category(category).label
    details = description.strip()
    return f"{category_label}: {details}" if details else category_label


@login_required
def business_development_needs(request, incubatee_id):
    _require_incubatee(request.user)
    business = _business_for_portal(request.user, incubatee_id)
    needs = (
        IncubateeBusinessDevelopmentNeed.objects.filter(incubatee=business)
        .select_related("assigned_to", "support_request")
        .prefetch_related("updates__updated_by", "updates__assigned_to", "updates__interaction")
    )
    if request.method == "POST":
        form = BusinessDevelopmentNeedsForm(request.POST, request.FILES)
        if form.is_valid():
            description = form.cleaned_data["description"].strip()
            with transaction.atomic():
                for category in form.cleaned_data["categories"]:
                    support_request = None
                    if form.cleaned_data["create_support_requests"]:
                        support_request = IncubateeSupportRequest.objects.create(
                            incubatee=business,
                            requested_by=request.user,
                            category=SUPPORT_REQUEST_CATEGORY_MAP[category],
                            description=_need_description(category, description),
                            evidence_original_filename="",
                        )
                    evidence = form.cleaned_data.get("evidence")
                    need = IncubateeBusinessDevelopmentNeed(
                        incubatee=business,
                        requested_by=request.user,
                        category=category,
                        description=description,
                        support_request=support_request,
                    )
                    if evidence:
                        evidence.seek(0)
                        need.evidence = evidence
                        need.evidence_original_filename = (
                            form.safe_filename(evidence)
                        )
                        need.evidence_content_type = form.cleaned_data[
                            "detected_content_type"
                        ]
                        need.evidence_size_bytes = evidence.size
                    need.save()
                    IncubateeBusinessDevelopmentNeedUpdate.objects.create(
                        need=need,
                        status=need.status,
                        updated_by=request.user,
                    )
                    log_event(
                        actor=request.user,
                        action=AuditEvent.Action.CREATE,
                        target=need,
                        resource="incubation.business_development_need",
                        metadata={
                            "business_id": str(business.pk),
                            "category": need.category,
                            "support_request_id": (
                                support_request.pk if support_request else None
                            ),
                        },
                        request=request,
                    )
                    if support_request:
                        log_event(
                            actor=request.user,
                            action=AuditEvent.Action.CREATE,
                            target=support_request,
                            resource="incubation.support_request",
                            metadata={
                                "business_id": str(business.pk),
                                "development_need_id": need.pk,
                            },
                            request=request,
                        )
            messages.success(request, "Your business development needs were recorded.")
            return redirect(
                "incubation:business_development_needs", incubatee_id=business.pk
            )
    else:
        form = BusinessDevelopmentNeedsForm()
    return render(
        request,
        "incubation/portal/business_development_needs.html",
        {"business": business, "needs": needs, "form": form},
    )


@login_required
def staff_business_development_needs(request):
    if not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    businesses = manageable_businesses(request.user)
    needs = (
        IncubateeBusinessDevelopmentNeed.objects.filter(incubatee__in=businesses)
        .select_related("incubatee", "requested_by", "assigned_to", "support_request")
        .prefetch_related("updates__updated_by", "updates__assigned_to")
        .order_by("status", "-identified_at", "-pk")
    )
    status = request.GET.get("status", "")
    valid_statuses = {value for value, _ in IncubateeBusinessDevelopmentNeed.Status.choices}
    if status in valid_statuses:
        needs = needs.filter(status=status)
    elif status:
        needs = IncubateeBusinessDevelopmentNeed.objects.none()
    page = Paginator(needs, 20).get_page(request.GET.get("page"))
    for need in page.object_list:
        need.update_form = BusinessDevelopmentNeedUpdateForm(
            business=need.incubatee,
            manager=request.user,
            initial={
                "status": need.status,
                "assigned_to": need.assigned_to_id,
                "resolution_date": need.resolution_date,
            },
        )
    return render(
        request,
        "incubation/account_management/business_development_needs.html",
        {
            "needs_page": page,
            "status_filter": status,
            "status_choices": IncubateeBusinessDevelopmentNeed.Status.choices,
        },
    )


@login_required
@require_POST
def staff_business_development_need_update(request, need_id):
    if not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    need = get_object_or_404(
        IncubateeBusinessDevelopmentNeed.objects.select_related("incubatee"),
        pk=need_id,
        incubatee__in=manageable_businesses(request.user),
    )
    form = BusinessDevelopmentNeedUpdateForm(
        request.POST,
        request.FILES,
        business=need.incubatee,
        manager=request.user,
    )
    if not form.is_valid():
        messages.error(request, "Enter valid support, outcome, status, and assignment details.")
        return redirect("incubation:staff_business_development_needs")

    with transaction.atomic():
        need = IncubateeBusinessDevelopmentNeed.objects.select_for_update().get(
            pk=need.pk
        )
        status = form.cleaned_data["status"]
        resolution_date = form.cleaned_data["resolution_date"]
        if status == IncubateeBusinessDevelopmentNeed.Status.RESOLVED:
            resolution_date = resolution_date or timezone.localdate()
        else:
            resolution_date = None
        evidence = form.cleaned_data.get("evidence")
        update = form.save(commit=False)
        update.need = need
        update.updated_by = request.user
        update.resolution_date = resolution_date
        if evidence:
            update.evidence_original_filename = form.safe_filename(evidence)
            update.evidence_content_type = form.cleaned_data["detected_content_type"]
            update.evidence_size_bytes = evidence.size
        update.save()

        need.status = status
        need.assigned_to = form.cleaned_data["assigned_to"]
        need.resolution_date = resolution_date
        need.save(update_fields=("status", "assigned_to", "resolution_date"))
        log_event(
            actor=request.user,
            action=AuditEvent.Action.UPDATE,
            target=need,
            resource="incubation.business_development_need",
            metadata={
                "business_id": str(need.incubatee_id),
                "status": need.status,
                "assigned_to": need.assigned_to_id,
                "resolution_date": (
                    need.resolution_date.isoformat() if need.resolution_date else None
                ),
                "update_id": update.pk,
                "mentoring_session_id": update.mentoring_session_id,
                "interaction_id": update.interaction_id,
            },
            request=request,
        )
    messages.success(request, "Assistance and need status were recorded.")
    return redirect("incubation:staff_business_development_needs")


@login_required
def business_development_need_evidence_download(request, incubatee_id, need_id):
    business = _business_for_portal(request.user, incubatee_id)
    need = get_object_or_404(
        IncubateeBusinessDevelopmentNeed, pk=need_id, incubatee=business
    )
    if not need.evidence:
        raise PermissionDenied("No evidence is attached to this need.")
    log_event(
        actor=request.user,
        action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
        target=need,
        resource="incubation.business_development_need",
        metadata={"business_id": str(business.pk), "evidence": "initial"},
        request=request,
    )
    return FileResponse(
        need.evidence.open("rb"),
        as_attachment=True,
        filename=need.evidence_original_filename or "supporting-evidence",
    )


@login_required
def business_development_need_update_evidence_download(
    request, incubatee_id, need_id, update_id
):
    business = _business_for_portal(request.user, incubatee_id)
    need = get_object_or_404(
        IncubateeBusinessDevelopmentNeed, pk=need_id, incubatee=business
    )
    update = get_object_or_404(
        IncubateeBusinessDevelopmentNeedUpdate,
        pk=update_id,
        need=need,
    )
    if not update.evidence:
        raise PermissionDenied("No evidence is attached to this update.")
    log_event(
        actor=request.user,
        action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
        target=update,
        resource="incubation.business_development_need_update",
        metadata={"business_id": str(business.pk), "need_id": need.pk},
        request=request,
    )
    return FileResponse(
        update.evidence.open("rb"),
        as_attachment=True,
        filename=update.evidence_original_filename or "supporting-evidence",
    )
