import hashlib
import logging
from datetime import date, datetime
from decimal import Decimal
from hmac import compare_digest
from zipfile import BadZipFile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.forms import formset_factory
from django.http import FileResponse, HttpResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl import load_workbook

from accounts.access import accessible_queryset, has_access, require_access, soft_delete_record
from accounts.models import Role, RoleGrant
from audit.models import AuditEvent
from audit.services import log_event, log_sensitive_record_view
from incubation.access import (
    can_manage_incubatee_documents,
    can_view_founder_data,
    is_aggregate_only,
    role_codes,
)
from incubation.baselines import capture_incubatee_baseline
from incubation.forms import (
    AssignmentForm,
    BusinessInformationForm,
    BusinessNeedsForm,
    CompanyInformationForm,
    FinancialBaselineForm,
    FounderForm,
    IncubateeConsentForm,
    IncubateeDocumentForm,
    IncubateeExitForm,
    IncubateeFollowUpForm,
    IncubateeImportForm,
    IncubateeMilestoneForm,
    LifecycleForm,
    MarketForm,
    MentoringSessionForm,
    MonthlyCheckInForm,
    MONTHLY_CHECKIN_FIELDS,
    QuarterlyReviewForm,
    TeamForm,
)
from incubation.models import (
    CheckInAccessLink,
    Cohort,
    FounderProfile,
    Incubatee,
    IncubateeAccessEvent,
    IncubateeConsent,
    IncubateeDocument,
    IncubateeExit,
    IncubateeFollowUp,
    IncubateeFounder,
    IncubateeMetricObservation,
    IncubateeMilestone,
    IncubateeObservationEvidence,
    IncubateeStatusHistory,
    MentoringSession,
    MonthlyCheckIn,
    MonthlyCheckInEvidence,
    Programme,
    QuarterlyReview,
)
from incubation.metrics import data_completeness, incubatee_metrics
from incubation.report_access import report_accessible_businesses
from incubation.security import decrypt_id_number, encrypt_id_number, id_number_fingerprint
from interactions.models import Interaction
from stakeholders.models import Stakeholder, StakeholderTypeAssignment

logger = logging.getLogger(__name__)
RESOURCE = RoleGrant.Resource.INCUBATEES
WIZARD_STEPS = {
    1: ("Company information", CompanyInformationForm),
    2: ("Business information", BusinessInformationForm),
    4: ("Team", TeamForm),
    5: ("Financial baseline", FinancialBaselineForm),
    6: ("Market", MarketForm),
    7: ("Business needs", BusinessNeedsForm),
}
IMPORT_FIELDS = {
    "legal_name",
    "trading_name",
    "cipc_registration_number",
    "date_registered",
    "entity_type",
    "vat_status",
    "tax_number",
    "bbbee_level",
    "physical_address",
    "province",
    "municipality",
    "urban_rural",
    "sic_code",
    "sub_sector",
    "products_services",
    "business_model",
    "business_stage",
    "formality",
}


def _get_incubatee(user, incubatee_id, action=RoleGrant.Action.READ):
    incubatee = get_object_or_404(
        Incubatee.objects.select_related(
            "programme", "cohort", "staff_owner", "owner", "created_by", "updated_by"
        ).prefetch_related("mentors"),
        pk=incubatee_id,
    )
    if not has_access(user, RESOURCE, action, incubatee):
        raise PermissionDenied
    return incubatee


def _log_access(request, incubatee, action, resource="", object_id=""):
    IncubateeAccessEvent.objects.create(
        actor=request.user,
        incubatee=incubatee,
        action=action,
        resource=resource,
        object_id=str(object_id),
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
    )


def _snapshot(incubatee, actor, source=None):
    return capture_incubatee_baseline(incubatee, actor, source)


def _active_incubatees(user, action=RoleGrant.Action.READ):
    return accessible_queryset(user, RESOURCE, action, Incubatee.objects.all())


def _is_sensitive_manager(user):
    return bool(role_codes(user) & {Role.Code.SUPER_ADMIN, Role.Code.ADMIN})


@login_required
def incubatee_list(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    if is_aggregate_only(request.user):
        report_businesses = report_accessible_businesses(request.user)
        if query:
            report_businesses = report_businesses.filter(
                Q(legal_name__icontains=query)
                | Q(trading_name__icontains=query)
            )
            if status in Incubatee.Status.values:
                report_businesses = report_businesses.filter(status=status)
        return render(
            request,
            "incubation/incubatee_list.html",
            {
                "report_only": True,
                "incubatees": report_businesses.select_related(
                    "programme", "cohort"
                ),
                "statuses": Incubatee.Status.choices,
                "selected_status": status,
                "query": query,
                "search_performed": bool(query),
            },
        )
    accessible_businesses = _active_incubatees(request.user).select_related(
        "programme", "cohort", "staff_owner"
    )
    incubatees = accessible_businesses.none()
    if query:
        incubatees = accessible_businesses.filter(
            Q(legal_name__icontains=query) | Q(trading_name__icontains=query)
        )
        if status in Incubatee.Status.values:
            incubatees = incubatees.filter(status=status)
    report_business_ids = set()
    if query:
        report_business_ids = set(
            report_accessible_businesses(
                request.user, queryset=incubatees
            ).values_list("pk", flat=True)
        )
    return render(
        request,
        "incubation/incubatee_list.html",
        {
            "incubatees": incubatees,
            "report_business_ids": report_business_ids,
            "statuses": Incubatee.Status.choices,
            "selected_status": status,
            "query": query,
            "search_performed": bool(query),
            "can_create": has_access(request.user, RESOURCE, RoleGrant.Action.CREATE),
            "can_import": has_access(request.user, RESOURCE, RoleGrant.Action.CREATE),
            "can_export": has_access(request.user, RESOURCE, RoleGrant.Action.READ),
        },
    )


@login_required
def incubatee_create(request):
    require_access(request.user, RESOURCE, RoleGrant.Action.CREATE)
    form = CompanyInformationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            incubatee = form.save(commit=False)
            incubatee.created_by = request.user
            incubatee.updated_by = request.user
            incubatee.owner = request.user
            incubatee.source = "manual"
            incubatee.save()
            incubatee.assigned_users.add(request.user)
            _snapshot(incubatee, request.user)
            IncubateeStatusHistory.objects.create(
                incubatee=incubatee,
                old_status="",
                new_status=incubatee.status,
                changed_by=request.user,
            )
        messages.success(request, "Incubatee created. Continue the onboarding wizard.")
        return redirect("incubation:onboarding_step", incubatee_id=incubatee.pk, step=2)
    return render(
        request,
        "incubation/incubatee_form.html",
        {"form": form, "title": "Start incubatee onboarding", "create": True},
    )


def _initial_founder_data(incubatee):
    initial = []
    for link in incubatee.founders.filter(is_active=True).select_related("founder__stakeholder"):
        profile = link.founder
        initial.append(
            {
                "founder_link_id": link.pk,
                "founder_name": profile.stakeholder.name,
                "id_number": decrypt_id_number(profile.id_number_encrypted),
                "gender": profile.gender,
                "date_of_birth": profile.date_of_birth,
                "race": profile.race,
                "disability_status": profile.disability_status,
                "highest_qualification": profile.highest_qualification,
                "prior_business_experience": profile.prior_business_experience,
                "ownership_percentage": link.ownership_percentage,
            }
        )
    return initial


def _founder_formset(request, incubatee):
    existing_count = incubatee.founders.filter(is_active=True).count()
    current_initial = _initial_founder_data(incubatee) if request.method == "GET" else None
    formset_class = formset_factory(
        FounderForm,
        extra=max(0, 10 - existing_count),
        max_num=max(10, existing_count),
        validate_max=True,
    )
    return formset_class(
        request.POST or None,
        prefix="founders",
        initial=current_initial,
        form_kwargs={},
    )


def _save_founders(request, incubatee, formset):
    existing_ids = set(incubatee.founders.filter(is_active=True).values_list("pk", flat=True))
    for form in formset.forms:
        if not form.cleaned_data or form.cleaned_data.get("DELETE"):
            link_id = form.cleaned_data.get("founder_link_id") if form.cleaned_data else None
            if link_id in existing_ids:
                IncubateeFounder.objects.filter(pk=link_id, incubatee=incubatee).update(
                    is_active=False,
                    updated_at=timezone.now(),
                    ended_at=timezone.now(),
                    ended_by=request.user,
                )
            continue
        data = form.cleaned_data
        name = (data.get("founder_name") or "").strip()
        if not name:
            continue
        link_id = data.get("founder_link_id")
        link = None
        profile = None
        if link_id:
            if link_id not in existing_ids:
                raise PermissionDenied("Founder relationship does not belong to this incubatee.")
            link = IncubateeFounder.objects.select_related("founder__stakeholder").get(
                pk=link_id, incubatee=incubatee
            )
            profile = link.founder
        fingerprint = id_number_fingerprint(data.get("id_number", ""))
        if fingerprint and profile is None:
            profile = FounderProfile.objects.filter(
                id_number_fingerprint=fingerprint
            ).select_related("stakeholder").first()
        if profile is None:
            stakeholder = Stakeholder.objects.create(
                name=name,
                title="Founder",
                email=None,
                owner=request.user,
                created_by=request.user,
            )
            StakeholderTypeAssignment.objects.get_or_create(
                stakeholder=stakeholder,
                relationship_type=Stakeholder.RelationshipType.FOUNDER,
                is_active=True,
                defaults={"assigned_by": request.user},
            )
            profile = FounderProfile.objects.create(stakeholder=stakeholder)
        profile.stakeholder.name = name
        profile.stakeholder.title = "Founder"
        profile.stakeholder.save(update_fields=("name", "title", "updated_at"))
        profile.gender = data.get("gender", "")
        profile.date_of_birth = data.get("date_of_birth")
        profile.race = data.get("race", "")
        profile.disability_status = data.get("disability_status", "")
        profile.highest_qualification = data.get("highest_qualification", "")
        profile.prior_business_experience = data.get("prior_business_experience", "")
        if data.get("id_number"):
            profile.id_number_encrypted = encrypt_id_number(data["id_number"])
            profile.id_number_fingerprint = fingerprint
        profile.save()
        if link is None:
            IncubateeFounder.objects.update_or_create(
                incubatee=incubatee,
                founder=profile,
                defaults={
                    "ownership_percentage": data.get("ownership_percentage"),
                    "is_active": True,
                },
            )
        else:
            link.ownership_percentage = data.get("ownership_percentage")
            link.save(update_fields=("ownership_percentage",))


def _record_consents(request, incubatee, form):
    for prefix, purpose in (
        ("popia", IncubateeConsent.Purpose.POPIA),
        ("funder", IncubateeConsent.Purpose.FUNDER_REPORTING),
    ):
        status = form.cleaned_data.get(f"{prefix}_status")
        consent_date = form.cleaned_data.get(f"{prefix}_date")
        method = form.cleaned_data.get(f"{prefix}_method", "").strip()
        if not status:
            continue
        latest = incubatee.consent_history.filter(purpose=purpose).first()
        if latest and (
            latest.status == status
            and latest.consent_date == consent_date
            and latest.method == method
        ):
            continue
        IncubateeConsent.objects.create(
            incubatee=incubatee,
            purpose=purpose,
            status=status,
            consent_date=consent_date,
            method=method,
            recorded_by=request.user,
        )


@login_required
def onboarding_step(request, incubatee_id, step):
    if step not in range(1, 9):
        raise PermissionDenied
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    if step in {3, 8} and not _is_sensitive_manager(request.user):
        raise PermissionDenied("Founder identity and consent data require an authorised administrator.")
    if step in {3, 8}:
        _log_access(
            request,
            incubatee,
            IncubateeAccessEvent.Action.PERSONAL_DATA_VIEW,
            resource=f"incubation.onboarding_step_{step}",
        )
        log_sensitive_record_view(
            request,
            incubatee,
            metadata={"onboarding_step": step},
        )

    if step == 3:
        formset = _founder_formset(request, incubatee)
        form = None
        valid = formset.is_valid() if request.method == "POST" else False
    elif step == 8:
        form = IncubateeConsentForm(request.POST or None, incubatee=incubatee)
        formset = None
        valid = form.is_valid() if request.method == "POST" else False
    else:
        title, form_class = WIZARD_STEPS[step]
        form = form_class(request.POST or None, instance=incubatee)
        formset = None
        valid = form.is_valid() if request.method == "POST" else False

    if request.method == "POST" and valid:
        with transaction.atomic():
            if step == 3:
                _save_founders(request, incubatee, formset)
            elif step == 8:
                _record_consents(request, incubatee, form)
            else:
                updated = form.save(commit=False)
                if step == 6:
                    updated.top_customer_types = form.customer_types()
                elif step == 7:
                    updated.top_support_needs = form.support_needs()
                updated.updated_by = request.user
                updated.save()
            if step != 8:
                incubatee.refresh_from_db()
                incubatee.updated_by = request.user
                incubatee.save(update_fields=("updated_by", "updated_at"))
                _snapshot(
                    incubatee,
                    request.user,
                    source=(
                        "initial_onboarding_baseline"
                        if step == 7
                        else None
                    ),
                )
        if step == 8:
            messages.success(request, "Onboarding consent responses were recorded.")
            return redirect("incubation:detail", incubatee_id=incubatee.pk)
        messages.success(request, f"{step_title(step)} saved.")
        return redirect(
            "incubation:onboarding_step",
            incubatee_id=incubatee.pk,
            step=min(step + 1, 8),
        )
    return render(
        request,
        "incubation/onboarding_step.html",
        {
            "incubatee": incubatee,
            "step": step,
            "steps": [(number, step_title(number)) for number in range(1, 9)],
            "title": step_title(step),
            "form": form,
            "formset": formset,
            "has_previous": step > 1,
            "has_next": step < 8,
            "duplicate_incubatee": getattr(form, "duplicate_incubatee", None),
            "can_view_founder_data": _is_sensitive_manager(request.user),
        },
    )


def step_title(step):
    if step == 3:
        return "Founders"
    if step == 8:
        return "Consent"
    return WIZARD_STEPS[step][0]


def _incubatee_timeline(user, incubatee, *, documents_visible):
    events = []
    for event in incubatee.status_history.select_related("changed_by"):
        events.append(
            {
                "date": event.changed_at,
                "kind": "Lifecycle",
                "title": f"Status changed to {event.get_new_status_display()}",
                "detail": f"Recorded by {event.changed_by.email}",
            }
        )
    for checkin in incubatee.monthly_checkins.filter(submitted_at__isnull=False):
        events.append(
            {
                "date": checkin.submitted_at,
                "kind": "Monthly check-in",
                "title": checkin.period_start.strftime("%B %Y"),
                "detail": f"Data completeness: {checkin.completeness_score or 0}%",
            }
        )
        if documents_visible:
            for evidence in checkin.evidence.all():
                events.append(
                    {
                        "date": evidence.uploaded_at,
                        "kind": "Check-in evidence",
                        "title": evidence.title,
                        "detail": evidence.original_filename,
                        "evidence_url": reverse(
                            "incubation:evidence_download",
                            args=(incubatee.pk, "checkin", evidence.pk),
                        ),
                    }
                )
    if documents_visible:
        for snapshot in incubatee.baseline_history.all():
            events.append(
                {
                    "date": snapshot.created_at,
                    "kind": "Profile data",
                    "title": "Baseline/profile values captured",
                    "detail": f"Source: {snapshot.source}",
                }
            )
    for review in incubatee.quarterly_reviews.select_related("submitted_by"):
        events.append(
            {
                "date": review.submitted_at,
                "kind": "Quarterly review",
                "title": f"Quarter starting {review.period_start}",
                "detail": "Quarterly data and compliance review recorded.",
            }
        )
    for milestone in incubatee.milestones.select_related("recorded_by"):
        events.append(
            {
                "date": milestone.milestone_date,
                "kind": "Milestone",
                "title": milestone.get_milestone_type_display(),
                "detail": milestone.description,
                "evidence_url": (
                    reverse("incubation:evidence_download", args=(incubatee.pk, "milestone", milestone.pk))
                    if documents_visible and milestone.evidence
                    else ""
                ),
            }
        )
    for session in incubatee.mentoring_sessions.select_related("mentor"):
        events.append(
            {
                "date": session.session_date,
                "kind": "Mentoring",
                "title": f"{session.mentoring_hours} hour(s)",
                "detail": session.topics,
            }
        )
    for interaction in Interaction.objects.filter(incubatees=incubatee).prefetch_related(
        "participants"
    ):
        if not has_access(
            user,
            RoleGrant.Resource.INTERACTIONS,
            RoleGrant.Action.READ,
            interaction,
        ):
            continue
        events.append(
            {
                "date": interaction.occurred_at,
                "kind": "Interaction",
                "title": interaction.get_interaction_type_display(),
                "detail": interaction.topics or interaction.outcome,
                "url": reverse("interactions:detail", args=(interaction.pk,)),
            }
        )
    for exit_record in incubatee.exit_records.all():
        events.append(
            {
                "date": exit_record.exit_date,
                "kind": "Exit",
                "title": exit_record.get_outcome_display(),
                "detail": exit_record.reason,
            }
        )
        for followup in exit_record.followups.filter(submitted_at__isnull=False):
            events.append(
                {
                    "date": followup.submitted_at,
                    "kind": f"{followup.months_after_exit}-month follow-up",
                    "title": followup.get_business_status_display() or "Submitted",
                    "detail": followup.notes,
                }
            )
    if documents_visible:
        for document in incubatee.documents.all():
            events.append(
                {
                    "date": document.uploaded_at,
                    "kind": "Document",
                    "title": document.title,
                    "detail": document.get_category_display(),
                    "evidence_url": reverse(
                        "incubation:document_download", args=(incubatee.pk, document.pk)
                    ),
                }
            )
        for review in incubatee.quarterly_reviews.exclude(management_accounts=""):
            events.append(
                {
                    "date": review.submitted_at,
                    "kind": "Management accounts",
                    "title": f"Quarter starting {review.period_start}",
                    "detail": "Private quarterly evidence",
                    "evidence_url": reverse(
                        "incubation:evidence_download", args=(incubatee.pk, "quarterly", review.pk)
                    ),
                }
            )
    return sorted(events, key=lambda event: event["date"], reverse=True)


@login_required
def incubatee_detail(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id)
    founder_data_visible = can_view_founder_data(request.user, incubatee)
    checkin_rows = [
        {"checkin": checkin, "completeness": data_completeness(checkin)}
        for checkin in incubatee.monthly_checkins.prefetch_related("observations")[:12]
    ]
    if founder_data_visible:
        _log_access(request, incubatee, IncubateeAccessEvent.Action.PERSONAL_DATA_VIEW)
    if founder_data_visible or incubatee.sensitivity in {"confidential", "restricted"}:
        log_sensitive_record_view(
            request,
            incubatee,
            metadata={"founder_personal_data": founder_data_visible},
        )
    return render(
        request,
        "incubation/incubatee_detail.html",
        {
            "incubatee": incubatee,
            "today": timezone.localdate(),
            "founders": incubatee.founders.filter(is_active=True).select_related("founder__stakeholder")
            if founder_data_visible
            else (),
            "consent_history": incubatee.consent_history.select_related("recorded_by")
            if founder_data_visible
            else (),
            "baseline_history": incubatee.baseline_history.select_related("changed_by")
            if founder_data_visible
            else (),
            "status_history": incubatee.status_history.select_related("changed_by"),
            "documents": incubatee.documents.select_related("uploaded_by")
            if can_manage_incubatee_documents(request.user, incubatee)
            else (),
            "monthly_checkins": checkin_rows,
            "quarterly_reviews": incubatee.quarterly_reviews.all(),
            "milestones": incubatee.milestones.all(),
            "mentoring_sessions": incubatee.mentoring_sessions.select_related("mentor"),
            "exit_records": incubatee.exit_records.prefetch_related("followups"),
            "timeline": _incubatee_timeline(
                request.user,
                incubatee,
                documents_visible=can_manage_incubatee_documents(request.user, incubatee),
            ),
            "derived_metrics": incubatee_metrics(incubatee),
            "founder_data_visible": founder_data_visible,
            "can_update": has_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, incubatee),
            "can_manage_assignments": _is_sensitive_manager(request.user),
            "can_manage_documents": can_manage_incubatee_documents(request.user, incubatee),
            "can_view_documents": can_manage_incubatee_documents(request.user, incubatee),
            "can_delete": has_access(request.user, RESOURCE, RoleGrant.Action.DELETE, incubatee),
            "is_sensitive_manager": _is_sensitive_manager(request.user),
        },
    )


@login_required
def assignment_update(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    if not _is_sensitive_manager(request.user):
        raise PermissionDenied("Only authorised administration roles can assign programme, cohort, owner, or mentors.")
    form = AssignmentForm(request.POST or None, instance=incubatee, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            previous_staff_owner = incubatee.staff_owner
            updated = form.save(commit=False)
            updated.updated_by = request.user
            updated.save()
            form.save_m2m()
            if (
                previous_staff_owner
                and previous_staff_owner.pk != updated.staff_owner_id
                and previous_staff_owner.pk != updated.owner_id
            ):
                updated.assigned_users.remove(previous_staff_owner)
            if updated.staff_owner_id:
                updated.assigned_users.add(updated.staff_owner)
            _snapshot(updated, request.user)
        messages.success(request, "Programme, cohort, owner, and mentor assignments saved.")
        return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/assignment_form.html",
        {"form": form, "incubatee": incubatee},
    )


@login_required
def lifecycle_update(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    form = LifecycleForm(request.POST or None, instance=incubatee)
    old_status = incubatee.status
    if request.method == "POST" and form.is_valid():
        new_status = form.cleaned_data["status"]
        if new_status != old_status:
            with transaction.atomic():
                incubatee = form.save(commit=False)
                incubatee.updated_by = request.user
                incubatee.save(update_fields=("status", "updated_by", "updated_at"))
                IncubateeStatusHistory.objects.create(
                    incubatee=incubatee,
                    old_status=old_status,
                    new_status=new_status,
                    changed_by=request.user,
                )
                _snapshot(incubatee, request.user)
        messages.success(request, "Incubatee lifecycle status updated.")
        return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/lifecycle_form.html",
        {"form": form, "incubatee": incubatee},
    )


@login_required
def incubatee_update(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    form = CompanyInformationForm(request.POST or None, instance=incubatee)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            updated = form.save(commit=False)
            updated.updated_by = request.user
            updated.save()
            _snapshot(updated, request.user)
        messages.success(request, "Company information updated.")
        return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/incubatee_form.html",
        {"form": form, "title": f"Edit {incubatee}", "incubatee": incubatee},
    )


@login_required
@require_POST
def incubatee_delete(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.DELETE)
    soft_delete_record(request.user, RESOURCE, incubatee)
    messages.success(request, "Incubatee was soft-deleted; its baseline and document history are retained.")
    return redirect("incubation:list")


@login_required
def incubatee_document_add(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.READ)
    if not can_manage_incubatee_documents(request.user, incubatee):
        raise PermissionDenied("Incubatee documents contain personal or financial data.")
    form = IncubateeDocumentForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        document = form.save(commit=False)
        document.incubatee = incubatee
        document.uploaded_by = request.user
        document.original_filename = form.safe_filename(form.cleaned_data["file"])
        document.content_type = form.cleaned_data["detected_content_type"]
        document.size_bytes = form.cleaned_data["file"].size
        document.save()
        messages.success(request, "Private incubatee document uploaded.")
        return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/document_form.html",
        {"form": form, "incubatee": incubatee},
    )


@login_required
def incubatee_document_download(request, incubatee_id, document_id):
    incubatee = _get_incubatee(request.user, incubatee_id)
    if not can_manage_incubatee_documents(
        request.user, incubatee
    ):
        raise PermissionDenied("Incubatee document access is restricted.")
    document = get_object_or_404(
        IncubateeDocument, pk=document_id, incubatee=incubatee
    )
    _log_access(
        request,
        incubatee,
        IncubateeAccessEvent.Action.DOCUMENT_DOWNLOAD,
        resource="incubation.document",
        object_id=document.pk,
    )
    log_event(
        actor=request.user,
        action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
        target=document,
        resource="incubation.document",
        request=request,
    )
    response = FileResponse(
        document.file.open("rb"),
        as_attachment=True,
        filename=document.original_filename,
        content_type=document.content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _checkin_error_response(status_code, message):
    response = HttpResponse(message, status=status_code, content_type="text/plain; charset=utf-8")
    return _secure_checkin_response(response)


def _secure_checkin_response(response):
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_http_methods(["GET", "POST"])
def public_checkin(request, link_id):
    token = (
        request.GET.get("token", "")
        if request.method == "GET"
        else request.POST.get("access_token", "")
    )
    link = get_object_or_404(
        CheckInAccessLink.objects.select_related("checkin", "checkin__incubatee"),
        pk=link_id,
    )
    supplied_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not compare_digest(link.token_digest, supplied_digest):
        raise PermissionDenied("The check-in link is invalid.")
    if link.expires_at <= timezone.now():
        return _checkin_error_response(410, "This check-in link has expired.")
    if link.used_at or link.checkin.submitted_at:
        return _checkin_error_response(409, "This check-in has already been submitted.")

    form = MonthlyCheckInForm(
        request.POST or None,
        request.FILES or None,
        initial={"access_token": token},
    )
    if request.method == "GET":
        IncubateeAccessEvent.objects.create(
            actor=None,
            incubatee=link.checkin.incubatee,
            action=IncubateeAccessEvent.Action.CHECKIN_LINK_VIEW,
            resource="incubation.monthly_checkin",
            object_id=str(link.checkin_id),
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
        )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                locked_link = CheckInAccessLink.objects.select_for_update().get(pk=link.pk)
                checkin = MonthlyCheckIn.objects.select_for_update().get(pk=link.checkin_id)
                if (
                    not compare_digest(locked_link.token_digest, supplied_digest)
                    or locked_link.expires_at <= timezone.now()
                    or locked_link.used_at is not None
                    or checkin.submitted_at is not None
                ):
                    return _checkin_error_response(409, "This check-in link is no longer available.")

                observations = {}
                for metric, values in form.observations():
                    observation = IncubateeMetricObservation.objects.create(
                        incubatee=checkin.incubatee,
                        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
                        period_start=checkin.period_start,
                        metric=metric,
                        source="monthly_checkin",
                        monthly_checkin=checkin,
                        **values,
                    )
                    observations[metric] = observation

                bank_statement = form.cleaned_data.get("bank_statement")
                if bank_statement:
                    revenue_observation = observations.get(
                        IncubateeMetricObservation.Metric.REVENUE_ZAR
                    )
                    MonthlyCheckInEvidence.objects.create(
                        checkin=checkin,
                        observation=revenue_observation,
                        incubatee=checkin.incubatee,
                        title="Monthly bank statement",
                        original_filename=IncubateeDocumentForm.safe_filename(bank_statement),
                        file=bank_statement,
                        content_type=form.cleaned_data["bank_statement_content_type"],
                        size_bytes=bank_statement.size,
                    )
                checkin.submitted_at = timezone.now()
                checkin.completeness_score = data_completeness(checkin)["score"]
                checkin.save(update_fields=("submitted_at", "completeness_score"))
                locked_link.used_at = timezone.now()
                locked_link.save(update_fields=("used_at",))
                IncubateeAccessEvent.objects.create(
                    actor=None,
                    incubatee=checkin.incubatee,
                    action=IncubateeAccessEvent.Action.CHECKIN_SUBMITTED,
                    resource="incubation.monthly_checkin",
                    object_id=str(checkin.pk),
                    ip_address=request.META.get("REMOTE_ADDR"),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
                )
        except IntegrityError:
            return _checkin_error_response(
                409,
                "A check-in for this reporting period already exists. No duplicate data was saved.",
            )
        return _secure_checkin_response(
            render(request, "incubation/checkin_submitted.html")
        )

    return _secure_checkin_response(
        render(
            request,
            "incubation/public_checkin.html",
            {"form": form, "period_start": link.checkin.period_start},
        )
    )


@login_required
def checkin_list(request):
    accessible_incubatees = _active_incubatees(request.user)
    checkins = MonthlyCheckIn.objects.filter(
        incubatee__in=accessible_incubatees
    ).select_related("incubatee")
    rows = [
        {"checkin": checkin, "completeness": data_completeness(checkin)}
        for checkin in checkins.prefetch_related("observations")
    ]
    return render(
        request,
        "incubation/checkin_list.html",
        {
            "checkins": rows,
            "today": timezone.localdate(),
            "can_export": has_access(
                request.user, RESOURCE, RoleGrant.Action.READ
            ),
        },
    )


@login_required
def quarterly_review_add(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    form = QuarterlyReviewForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        period = form.cleaned_data["period_start"]
        if QuarterlyReview.objects.filter(incubatee=incubatee, period_start=period).exists():
            form.add_error("period_start", "A quarterly review already exists for this period.")
        elif form.cleaned_data.get("management_accounts") and not can_manage_incubatee_documents(
            request.user, incubatee
        ):
            form.add_error(
                "management_accounts",
                "Management account uploads are restricted to authorised administration roles.",
            )
        else:
            try:
                with transaction.atomic():
                    uploaded = form.cleaned_data.get("management_accounts")
                    review = QuarterlyReview(
                        incubatee=incubatee,
                        period_start=period,
                        tax_clearance_status=form.cleaned_data["tax_clearance_status"].strip(),
                        cipc_annual_return_status=form.cleaned_data[
                            "cipc_annual_return_status"
                        ].strip(),
                        management_accounts=uploaded or "",
                        submitted_by=request.user,
                        source="staff_form",
                    )
                    if uploaded:
                        review.original_filename = form.safe_filename(uploaded)
                        review.content_type = form.cleaned_data["management_accounts_content_type"]
                        review.size_bytes = uploaded.size
                    review.save()
                    score_fields = (
                        IncubateeMetricObservation.Metric.FINANCE_SCORE,
                        IncubateeMetricObservation.Metric.MARKETING_SCORE,
                        IncubateeMetricObservation.Metric.OPERATIONS_SCORE,
                        IncubateeMetricObservation.Metric.COMPLIANCE_SCORE,
                        IncubateeMetricObservation.Metric.TECHNOLOGY_SCORE,
                        IncubateeMetricObservation.Metric.LEADERSHIP_SCORE,
                    )
                    for field_name, metric in zip(
                        ("finance_score", "marketing_score", "operations_score",
                         "compliance_score", "technology_score", "leadership_score"),
                        score_fields,
                    ):
                        value = form.cleaned_data.get(field_name, "").strip()
                        if value:
                            IncubateeMetricObservation.objects.create(
                                incubatee=incubatee,
                                period_type=IncubateeMetricObservation.PeriodType.QUARTERLY,
                                period_start=period,
                                metric=metric,
                                integer_value=int(value),
                                source="quarterly_review",
                                quarterly_review=review,
                                recorded_by=request.user,
                            )
                    for value, metric in (
                        (review.tax_clearance_status, IncubateeMetricObservation.Metric.TAX_CLEARANCE_STATUS),
                        (review.cipc_annual_return_status, IncubateeMetricObservation.Metric.CIPC_RETURN_STATUS),
                    ):
                        if value:
                            IncubateeMetricObservation.objects.create(
                                incubatee=incubatee,
                                period_type=IncubateeMetricObservation.PeriodType.QUARTERLY,
                                period_start=period,
                                metric=metric,
                                text_value=value,
                                source="quarterly_review",
                                quarterly_review=review,
                                recorded_by=request.user,
                            )
            except IntegrityError:
                form.add_error("period_start", "A quarterly review already exists for this period.")
            else:
                messages.success(request, "Quarterly review and metric observations recorded.")
                return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/quarterly_review_form.html",
        {"form": form, "incubatee": incubatee},
    )


@login_required
def milestone_add(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    form = IncubateeMilestoneForm(request.POST or None, request.FILES or None, incubatee=incubatee)
    if request.method == "POST" and form.is_valid():
        evidence = form.cleaned_data.get("evidence")
        if evidence and not can_manage_incubatee_documents(request.user, incubatee):
            form.add_error(
                "evidence",
                "Milestone evidence uploads are restricted to authorised administration roles.",
            )
        else:
            milestone = form.save(commit=False)
            milestone.incubatee = incubatee
            milestone.recorded_by = request.user
            if evidence:
                milestone.original_filename = form.safe_filename(evidence)
                milestone.content_type = form.cleaned_data["evidence_content_type"]
                milestone.size_bytes = evidence.size
            milestone.save()
            messages.success(request, "Milestone recorded.")
            return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/milestone_form.html",
        {"form": form, "incubatee": incubatee},
    )


@login_required
def mentoring_session_add(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    form = MentoringSessionForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        session = form.save(commit=False)
        session.incubatee = incubatee
        session.recorded_by = request.user
        session.save()
        messages.success(request, "Mentoring session recorded.")
        return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/mentoring_form.html",
        {"form": form, "incubatee": incubatee},
    )


@login_required
def exit_record_add(request, incubatee_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    form = IncubateeExitForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                exit_record = form.save(commit=False)
                exit_record.incubatee = incubatee
                exit_record.recorded_by = request.user
                exit_record.save()
                old_status = incubatee.status
                new_status = exit_record.outcome
                if old_status != new_status:
                    incubatee.status = new_status
                    incubatee.updated_by = request.user
                    incubatee.save(update_fields=("status", "updated_by", "updated_at"))
                    IncubateeStatusHistory.objects.create(
                        incubatee=incubatee,
                        old_status=old_status,
                        new_status=new_status,
                        changed_by=request.user,
                    )
                    _snapshot(incubatee, request.user)
        except IntegrityError:
            form.add_error("exit_date", "An exit record already exists for this date.")
        else:
            messages.success(request, "Graduation/exit record saved. Six- and twelve-month follow-ups will be queued when due.")
            return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/exit_form.html",
        {"form": form, "incubatee": incubatee},
    )


@login_required
def followup_submit(request, incubatee_id, followup_id):
    incubatee = _get_incubatee(request.user, incubatee_id, RoleGrant.Action.UPDATE)
    followup = get_object_or_404(
        IncubateeFollowUp.objects.select_related("exit_record"),
        pk=followup_id,
        exit_record__incubatee=incubatee,
    )
    if followup.submitted_at:
        return HttpResponse("This follow-up survey has already been submitted.", status=409)
    form = IncubateeFollowUpForm(request.POST or None, instance=followup)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.submitted_by = request.user
        saved.submitted_at = timezone.now()
        saved.save()
        messages.success(request, "Follow-up survey submitted.")
        return redirect("incubation:detail", incubatee_id=incubatee.pk)
    return render(
        request,
        "incubation/followup_form.html",
        {"form": form, "incubatee": incubatee, "followup": followup},
    )


@login_required
def evidence_download(request, incubatee_id, evidence_type, evidence_id):
    incubatee = _get_incubatee(request.user, incubatee_id)
    if not can_manage_incubatee_documents(request.user, incubatee):
        raise PermissionDenied("Incubatee evidence access is restricted.")
    if evidence_type == "milestone":
        record = get_object_or_404(IncubateeMilestone, pk=evidence_id, incubatee=incubatee)
        file_field = record.evidence
        filename = record.original_filename
        content_type = record.content_type
    elif evidence_type == "quarterly":
        record = get_object_or_404(QuarterlyReview, pk=evidence_id, incubatee=incubatee)
        file_field = record.management_accounts
        filename = record.original_filename
        content_type = record.content_type
    elif evidence_type == "observation":
        record = get_object_or_404(
            IncubateeObservationEvidence, pk=evidence_id, incubatee=incubatee
        )
        file_field = record.file
        filename = record.original_filename
        content_type = record.content_type
    elif evidence_type == "checkin":
        record = get_object_or_404(
            MonthlyCheckInEvidence, pk=evidence_id, incubatee=incubatee
        )
        file_field = record.file
        filename = record.original_filename
        content_type = record.content_type
    else:
        raise PermissionDenied("Unknown evidence type.")
    _log_access(
        request,
        incubatee,
        IncubateeAccessEvent.Action.DOCUMENT_DOWNLOAD,
        resource=f"incubation.{evidence_type}_evidence",
        object_id=evidence_id,
    )
    log_event(
        actor=request.user,
        action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
        target=record,
        resource=f"incubation.{evidence_type}_evidence",
        request=request,
    )
    response = FileResponse(
        file_field.open("rb"),
        as_attachment=True,
        filename=filename,
        content_type=content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    return response


@login_required
def incubatee_import(request):
    require_access(request.user, RESOURCE, RoleGrant.Action.CREATE)
    if not _is_sensitive_manager(request.user):
        raise PermissionDenied("Bulk imports require an authorised administration role.")
    if request.method == "POST" and request.POST.get("confirm") == "yes":
        rows = request.session.pop("incubatee_import_rows", [])
        if not rows:
            messages.error(request, "The import preview expired. Upload the workbook again.")
            return redirect("incubation:import")
        created = 0
        with transaction.atomic():
            for row in rows:
                incubatee = Incubatee.objects.create(
                    **row,
                    created_by=request.user,
                    updated_by=request.user,
                    owner=request.user,
                    source="excel_import",
                )
                incubatee.assigned_users.add(request.user)
                IncubateeStatusHistory.objects.create(
                    incubatee=incubatee,
                    old_status="",
                    new_status=incubatee.status,
                    changed_by=request.user,
                )
                _snapshot(incubatee, request.user, source="excel_import")
                created += 1
        messages.success(request, f"Imported {created} incubatee records.")
        return redirect("incubation:list")

    form = IncubateeImportForm(request.POST or None, request.FILES or None)
    preview_rows = []
    errors = []
    if request.method == "POST" and form.is_valid():
        try:
            workbook = load_workbook(
                form.cleaned_data["workbook"], read_only=True, data_only=True
            )
        except (InvalidFileException, BadZipFile, OSError):
            form.add_error("workbook", "The uploaded file is not a readable .xlsx workbook.")
            return render(
                request,
                "incubation/import_form.html",
                {"form": form, "errors": errors},
            )
        worksheet = workbook.active
        values = worksheet.iter_rows(values_only=True)
        headers = next(values, None)
        header_columns = [
            (index, str(value).strip().lower())
            for index, value in enumerate(headers or ())
            if value is not None and str(value).strip()
        ]
        normalized_headers = [header for _, header in header_columns]
        duplicate_headers = {
            header for header in normalized_headers if normalized_headers.count(header) > 1
        }
        unsupported_headers = set(normalized_headers) - IMPORT_FIELDS
        if duplicate_headers:
            errors.append(
                {"row": 1, "error": f"Duplicate headers: {', '.join(sorted(duplicate_headers))}"}
            )
        elif unsupported_headers:
            errors.append(
                {"row": 1, "error": f"Unsupported columns: {', '.join(sorted(unsupported_headers))}"}
            )
        elif "legal_name" not in normalized_headers:
            errors.append({"row": 1, "error": "The first row must include a legal_name header."})
        elif worksheet.max_row > 1001:
            errors.append({"row": 1, "error": "A workbook may contain at most 1,000 data rows."})
        else:
            seen_names = set()
            seen_registrations = set()
            for row_number, values_row in enumerate(values, start=2):
                data = {
                    header: values_row[index] if index < len(values_row) else None
                    for index, header in header_columns
                }
                if not any(value is not None for value in values_row):
                    continue
                legal_name = str(data.get("legal_name") or "").strip()
                if not legal_name:
                    errors.append({"row": row_number, "error": "legal_name is required."})
                    continue
                normalized_name = legal_name.casefold()
                if (
                    normalized_name in seen_names
                    or Incubatee.objects.filter(legal_name__iexact=legal_name).exists()
                ):
                    errors.append(
                        {"row": row_number, "error": "Potential duplicate legal name already exists."}
                    )
                    continue
                registration = data.get("cipc_registration_number")
                registration = str(registration).strip() if registration is not None else ""
                if registration and Incubatee.objects.filter(
                    cipc_registration_number__iexact=registration
                ).exists():
                    errors.append({"row": row_number, "error": "CIPC registration number already exists."})
                    continue
                normalized_registration = registration.casefold()
                if normalized_registration and normalized_registration in seen_registrations:
                    errors.append(
                        {"row": row_number, "error": "CIPC registration number is duplicated in this workbook."}
                    )
                    continue
                converted = {}
                invalid = None
                for field, value in data.items():
                    if field not in IMPORT_FIELDS or value is None:
                        continue
                    value = value.strip() if isinstance(value, str) else value
                    if field == "date_registered" and value:
                        try:
                            value = value.date() if isinstance(value, datetime) else (
                                value if isinstance(value, date) else date.fromisoformat(str(value))
                            )
                        except (TypeError, ValueError):
                            invalid = "date_registered must be an Excel date or ISO YYYY-MM-DD."
                            break
                    if field == "cipc_registration_number":
                        value = str(value).strip() or None
                    elif field not in {"date_registered"}:
                        value = str(value).strip()
                    converted[field] = value
                if invalid:
                    errors.append({"row": row_number, "error": invalid})
                    continue
                candidate = Incubatee(
                    **converted,
                    created_by=request.user,
                    updated_by=request.user,
                    owner=request.user,
                    source="excel_import",
                )
                try:
                    candidate.full_clean()
                except ValidationError as error:
                    errors.append(
                        {"row": row_number, "error": "; ".join(error.messages)}
                    )
                    continue
                if isinstance(converted.get("date_registered"), date):
                    converted["date_registered"] = converted["date_registered"].isoformat()
                preview_rows.append({"row": row_number, "data": converted})
                seen_names.add(normalized_name)
                if normalized_registration:
                    seen_registrations.add(normalized_registration)
        workbook.close()
        request.session["incubatee_import_rows"] = [row["data"] for row in preview_rows]
        request.session.modified = True
        return render(
            request,
            "incubation/import_preview.html",
            {"form": form, "preview_rows": preview_rows, "errors": errors},
        )
    return render(
        request,
        "incubation/import_form.html",
        {"form": form, "errors": errors},
    )
