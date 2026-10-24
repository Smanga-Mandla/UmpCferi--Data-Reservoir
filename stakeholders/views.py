from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.access import accessible_queryset, has_access, require_access, soft_delete_record
from accounts.models import Role, RoleGrant
from audit.services import log_sensitive_record_view
from partners.models import MOU, Partner
from stakeholders.access import can_manage_sensitive_profiles, can_view_contact_details
from stakeholders.forms import POPIAConsentForm, StakeholderDetailsForm, StakeholderForm
from stakeholders.models import Stakeholder


def _stakeholder_queryset(user, action=RoleGrant.Action.READ):
    return accessible_queryset(
        user,
        RoleGrant.Resource.STAKEHOLDERS,
        action,
        Stakeholder.objects.select_related("owner", "created_by"),
    )


def _get_stakeholder(user, stakeholder_id, action=RoleGrant.Action.READ):
    stakeholder = get_object_or_404(Stakeholder.objects.all(), pk=stakeholder_id)
    if not has_access(user, RoleGrant.Resource.STAKEHOLDERS, action, stakeholder):
        raise PermissionDenied
    return stakeholder


def _relationship_history(stakeholder):
    return stakeholder.type_assignments.select_related(
        "assigned_by", "ended_by"
    )


def _active_relationship_types(stakeholder):
    return set(
        stakeholder.type_assignments.filter(is_active=True).values_list(
            "relationship_type", flat=True
        )
    )


@login_required
def stakeholder_list(request):
    stakeholders = _stakeholder_queryset(request.user).prefetch_related(
        "type_assignments"
    )
    query = request.GET.get("q", "").strip()
    role_codes = set(request.user.roles.values_list("code", flat=True))
    can_search_contact_fields = request.user.is_superuser or bool(
        role_codes
        & {Role.Code.SUPER_ADMIN, Role.Code.ADMIN, Role.Code.CFERI_STAFF}
    )
    if query:
        search = Q(name__icontains=query) | Q(title__icontains=query)
        if can_search_contact_fields:
            search |= Q(email__icontains=query) | Q(phone__icontains=query)
        stakeholders = stakeholders.filter(search)
    contacts = list(stakeholders)
    for contact in contacts:
        contact.show_contact_details = can_view_contact_details(request.user, contact)
        contact.active_relationship_types = [
            assignment.get_relationship_type_display()
            for assignment in contact.type_assignments.all()
            if assignment.is_active
        ]
    return render(
        request,
        "stakeholders/stakeholder_list.html",
        {
            "stakeholders": contacts,
            "query": query,
            "can_search_contact_fields": can_search_contact_fields,
            "can_create": has_access(
                request.user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.CREATE,
            ),
            "can_export": has_access(
                request.user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.READ,
            ),
        },
    )


def _stakeholder_form_context(request, form, details_form, title, stakeholder=None):
    return {
        "form": form,
        "details_form": details_form,
        "can_manage_sensitive_profiles": can_manage_sensitive_profiles(request.user),
        "title": title,
        "stakeholder": stakeholder,
    }


@login_required
def stakeholder_create(request):
    partner_id = request.GET.get("partner")
    if partner_id:
        partner = get_object_or_404(Partner.objects.all(), pk=partner_id)
        if not has_access(
            request.user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.UPDATE,
            partner,
        ):
            raise PermissionDenied
    require_access(
        request.user,
        RoleGrant.Resource.STAKEHOLDERS,
        RoleGrant.Action.CREATE,
    )
    form = StakeholderForm(request.POST or None, user=request.user)
    details_form = (
        StakeholderDetailsForm(request.POST or None, user=request.user)
        if can_manage_sensitive_profiles(request.user)
        else None
    )
    if request.method == "POST" and form.is_valid() and (
        details_form is None or details_form.is_valid()
    ):
        with transaction.atomic():
            stakeholder = form.save(commit=False)
            stakeholder.owner = request.user
            stakeholder.created_by = request.user
            require_access(
                request.user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.CREATE,
                stakeholder,
            )
            stakeholder.save()
            form.save_relationships(stakeholder)
            if details_form:
                details_form.save_profiles(stakeholder)
        if form.duplicate_matches:
            messages.warning(
                request,
                "This record matches existing contact details. Review the linked stakeholder before using it.",
            )
        messages.success(request, "Stakeholder record added.")
        if partner_id:
            return redirect("partners:representative_add", partner_id=partner_id)
        return redirect("stakeholders:detail", stakeholder_id=stakeholder.pk)
    return render(
        request,
        "stakeholders/stakeholder_form.html",
        _stakeholder_form_context(request, form, details_form, "Add stakeholder"),
    )


@login_required
def stakeholder_detail(request, stakeholder_id):
    stakeholder = _get_stakeholder(request.user, stakeholder_id)
    can_view_profiles = can_manage_sensitive_profiles(request.user)
    if can_view_profiles:
        log_sensitive_record_view(
            request,
            stakeholder,
            metadata={"sensitive_profile_fields": True},
        )
    relationship_types = _active_relationship_types(stakeholder)
    staff_profile = getattr(stakeholder, "staff_profile", None)
    assigned_incubatees = []
    if can_view_profiles and staff_profile:
        for assignment in staff_profile.incubatee_assignments.filter(
            is_active=True
        ).select_related("incubatee"):
            if has_access(
                request.user,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.READ,
                assignment.incubatee,
            ):
                assigned_incubatees.append(assignment.incubatee)
    partner_links = []
    for link in stakeholder.partner_representative_links.filter(
        is_active=True
    ).select_related("partner"):
        if has_access(
            request.user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.READ,
            link.partner,
        ):
            partner_links.append(link)
    readable_mous = []
    for mou in MOU.objects.filter(is_deleted=False).filter(
        Q(primary_representative=stakeholder) | Q(secondary_representative=stakeholder)
    ).select_related("partner"):
        if has_access(
            request.user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.READ,
            mou,
        ) and has_access(
            request.user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.READ,
            mou.partner,
        ):
            readable_mous.append(mou)
    return render(
        request,
        "stakeholders/stakeholder_detail.html",
        {
            "stakeholder": stakeholder,
            "can_update": has_access(
                request.user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.UPDATE,
                stakeholder,
            ),
            "can_delete": has_access(
                request.user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.DELETE,
                stakeholder,
            ),
            "can_view_contact_details": can_view_contact_details(
                request.user, stakeholder
            ),
            "can_view_profiles": can_view_profiles,
            "can_add_consent": can_view_profiles
            and has_access(
                request.user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.UPDATE,
                stakeholder,
            ),
            "relationship_history": _relationship_history(stakeholder),
            "partner_links": partner_links,
            "mous": readable_mous,
            "linked": bool(partner_links or readable_mous),
            "board_profile": getattr(stakeholder, "board_profile", None)
            if can_view_profiles
            and Stakeholder.RelationshipType.BOARD_MEMBER in relationship_types
            else None,
            "staff_profile": getattr(stakeholder, "staff_profile", None)
            if can_view_profiles
            and relationship_types
            & {
                Stakeholder.RelationshipType.STAFF,
                Stakeholder.RelationshipType.FREELANCER,
            }
            else None,
            "assigned_incubatees": assigned_incubatees,
            "consent_history": stakeholder.consent_history.select_related("recorded_by")
            if can_view_profiles
            else (),
        },
    )


@login_required
def stakeholder_update(request, stakeholder_id):
    stakeholder = _get_stakeholder(
        request.user, stakeholder_id, RoleGrant.Action.UPDATE
    )
    form = StakeholderForm(
        request.POST or None, instance=stakeholder, user=request.user
    )
    details_form = (
        StakeholderDetailsForm(
            request.POST or None,
            stakeholder=stakeholder,
            user=request.user,
        )
        if can_manage_sensitive_profiles(request.user)
        else None
    )
    if request.method == "POST" and form.is_valid() and (
        details_form is None or details_form.is_valid()
    ):
        with transaction.atomic():
            updated_stakeholder = form.save(commit=False)
            require_access(
                request.user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.UPDATE,
                updated_stakeholder,
            )
            updated_stakeholder.save()
            form.save_relationships(updated_stakeholder)
            if details_form:
                details_form.save_profiles(updated_stakeholder)
        if form.duplicate_matches:
            messages.warning(
                request,
                "This record matches existing contact details. Review the linked stakeholder before using it.",
            )
        messages.success(request, "Stakeholder record updated.")
        return redirect("stakeholders:detail", stakeholder_id=stakeholder.pk)
    return render(
        request,
        "stakeholders/stakeholder_form.html",
        _stakeholder_form_context(
            request, form, details_form, "Edit stakeholder", stakeholder
        ),
    )


@login_required
def consent_add(request, stakeholder_id):
    stakeholder = _get_stakeholder(
        request.user, stakeholder_id, RoleGrant.Action.UPDATE
    )
    if not can_manage_sensitive_profiles(request.user):
        raise PermissionDenied
    form = POPIAConsentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        consent = form.save(commit=False)
        consent.stakeholder = stakeholder
        consent.recorded_by = request.user
        consent.save()
        messages.success(request, "POPIA consent history recorded.")
        return redirect("stakeholders:detail", stakeholder_id=stakeholder.pk)
    return render(
        request,
        "stakeholders/consent_form.html",
        {"form": form, "stakeholder": stakeholder},
    )


@login_required
@require_POST
def stakeholder_delete(request, stakeholder_id):
    stakeholder = _get_stakeholder(
        request.user, stakeholder_id, RoleGrant.Action.DELETE
    )
    if (
        stakeholder.partner_representative_links.filter(is_active=True).exists()
        or stakeholder.primary_mous.filter(is_deleted=False).exists()
        or stakeholder.secondary_mous.filter(is_deleted=False).exists()
    ):
        raise PermissionDenied(
            "Unlink this contact from partner organisations and MOUs before deleting it."
        )
    with transaction.atomic():
        soft_delete_record(
            request.user,
            RoleGrant.Resource.STAKEHOLDERS,
            stakeholder,
        )
    messages.success(request, "Stakeholder record was soft-deleted.")
    return redirect("stakeholders:list")
