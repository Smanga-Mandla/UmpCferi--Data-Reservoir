from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Role
from audit.models import AuditEvent
from audit.services import log_event
from incubation.account_access import can_manage_incubatee_accounts, manageable_businesses
from incubation.models import (
    BusinessDocument,
    IncubateePortalNotification,
    IncubateeSupportRequest,
    IncubateeSupportRequestUpdate,
    MonthlyCheckIn,
)
from incubation.portal_notifications import (
    notify_business_accounts,
    notify_support_request,
)
from incubation.portal_views import _business_for_portal, _require_incubatee
from incubation.support_forms import (
    IncubateeSupportRequestForm,
    SupportRequestReviewForm,
    eligible_request_assignees,
)
from interactions.models import Interaction


def _portal_business(request, incubatee_id):
    _require_incubatee(request.user)
    return _business_for_portal(request.user, incubatee_id)


@login_required
def support_request_list(request, incubatee_id):
    business = _portal_business(request, incubatee_id)
    requests = business.support_requests.select_related("requested_by", "assigned_to").prefetch_related("updates")
    return render(
        request,
        "incubation/portal/support_requests.html",
        {
            "business": business,
            "requests": requests,
            "form": IncubateeSupportRequestForm(),
        },
    )


@login_required
@require_POST
def support_request_submit(request, incubatee_id):
    business = _portal_business(request, incubatee_id)
    form = IncubateeSupportRequestForm(request.POST, request.FILES)
    if form.is_valid():
        with transaction.atomic():
            support_request = form.save(commit=False)
            support_request.incubatee = business
            support_request.requested_by = request.user
            evidence = form.cleaned_data.get("evidence")
            if evidence:
                support_request.evidence_original_filename = (
                    form.safe_filename(evidence)
                )
                support_request.evidence_content_type = form.cleaned_data[
                    "detected_content_type"
                ]
                support_request.evidence_size_bytes = evidence.size
            support_request.save()
            log_event(
                actor=request.user,
                action=AuditEvent.Action.CREATE,
                target=support_request,
                resource="incubation.support_request",
                metadata={
                    "business_id": str(business.pk),
                    "category": support_request.category,
                },
                request=request,
            )
        messages.success(request, "Your support request was submitted.")
        return redirect(
            "incubation:support_request_list", incubatee_id=business.pk
        )
    return render(
        request,
        "incubation/portal/support_requests.html",
        {
            "business": business,
            "requests": business.support_requests.select_related(
                "requested_by", "assigned_to"
            ).prefetch_related("updates"),
            "form": form,
        },
    )


@login_required
def support_request_review_list(request):
    if not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    requests = list(IncubateeSupportRequest.objects.filter(
        incubatee__in=manageable_businesses(request.user)
    ).select_related("incubatee", "requested_by", "assigned_to").prefetch_related(
        "updates"
    ))
    for support_request in requests:
        eligible_staff = eligible_request_assignees(
            support_request.incubatee, request.user
        )
        support_request.review_form = SupportRequestReviewForm(
            initial={
                "status": support_request.status,
                "assigned_to": support_request.assigned_to_id,
            },
            eligible_staff=eligible_staff,
        )
    return render(
        request,
        "incubation/portal/support_request_review_list.html",
        {"requests": requests},
    )


@login_required
@require_POST
def support_request_review(request, request_id):
    if not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    support_request = get_object_or_404(
        IncubateeSupportRequest.objects.select_related("incubatee"),
        pk=request_id,
        incubatee__in=manageable_businesses(request.user),
    )
    form = SupportRequestReviewForm(
        request.POST,
        eligible_staff=eligible_request_assignees(
            support_request.incubatee, request.user
        ),
    )
    if not form.is_valid():
        messages.error(request, "Enter a valid status, assignee, and response.")
        return redirect("incubation:support_request_review_list")

    with transaction.atomic():
        support_request = IncubateeSupportRequest.objects.select_for_update().get(
            pk=support_request.pk
        )
        previous_status = support_request.status
        previous_assignee_id = support_request.assigned_to_id
        support_request.status = form.cleaned_data["status"]
        support_request.assigned_to = form.cleaned_data["assigned_to"]
        support_request.save(update_fields=("status", "assigned_to", "updated_at"))
        update = IncubateeSupportRequestUpdate.objects.create(
            support_request=support_request,
            status=support_request.status,
            assigned_to=support_request.assigned_to,
            response=form.cleaned_data["response"].strip(),
            updated_by=request.user,
        )
        log_event(
            actor=request.user,
            action=AuditEvent.Action.UPDATE,
            target=support_request,
            resource="incubation.support_request",
            metadata={
                "business_id": str(support_request.incubatee_id),
                "status": support_request.status,
                "previous_status": previous_status,
                "assigned_to": support_request.assigned_to_id,
                "previous_assignee": previous_assignee_id,
                "update_id": update.pk,
            },
            request=request,
        )
        notify_support_request(
            support_request,
            status_changed=previous_status != support_request.status,
            response=update.response,
        )
    messages.success(request, "Support request response recorded.")
    return redirect("incubation:support_request_review_list")


@login_required
def support_request_evidence_download(request, incubatee_id, request_id):
    business = _business_for_portal(request.user, incubatee_id)
    if request.user.roles.filter(code=Role.Code.INCUBATEE).exists():
        support_request = get_object_or_404(
            IncubateeSupportRequest,
            pk=request_id,
            incubatee=business,
        )
    elif can_manage_incubatee_accounts(request.user):
        support_request = get_object_or_404(
            IncubateeSupportRequest,
            pk=request_id,
            incubatee=business,
        )
    else:
        raise PermissionDenied
    if not support_request.evidence:
        raise PermissionDenied
    log_event(
        actor=request.user,
        action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
        target=support_request,
        resource="incubation.support_request_evidence",
        metadata={"business_id": str(business.pk)},
        request=request,
    )
    response = FileResponse(
        support_request.evidence.open("rb"),
        as_attachment=True,
        filename=support_request.evidence_original_filename or "support-evidence",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def mentorship_portal(request, incubatee_id):
    business = _portal_business(request, incubatee_id)
    today = timezone.localdate()
    sessions = business.mentoring_sessions.select_related("mentor").order_by(
        "session_date", "created_at"
    )
    interactions = Interaction.objects.filter(
        incubatees=business,
        interaction_type=Interaction.Type.MENTORING,
        sensitivity__in=("public", "internal"),
    ).annotate(linked_business_count=Count("incubatees", distinct=True)).filter(
        linked_business_count=1
    ).order_by("-occurred_at")
    return render(
        request,
        "incubation/portal/mentorship.html",
        {
            "business": business,
            "mentors": business.mentors.all(),
            "today": today,
            "sessions": sessions,
            "upcoming_sessions": sessions.filter(session_date__gte=today),
            "past_sessions": sessions.filter(session_date__lt=today),
            "interactions": interactions,
        },
    )


def _notification_events(business, user):
    today = timezone.localdate()
    events = []
    checkins = MonthlyCheckIn.objects.filter(
        incubatee=business,
        sent_at__isnull=False,
        submitted_at__isnull=True,
        due_date__isnull=False,
    ).order_by("due_date")
    for checkin in checkins:
        if checkin.due_date < today:
            title = f"Monthly progress is overdue for {checkin.period_start:%B %Y}"
        else:
            title = f"Monthly progress is due {checkin.due_date:%j %B %Y}"
        events.append(
            {
                "type": IncubateePortalNotification.Type.PROGRESS_DEADLINE,
                "title": title,
                "message": "",
                "date": checkin.due_date,
                "read": False,
            }
        )

    documents = BusinessDocument.objects.filter(
        incubatee=business,
        status=BusinessDocument.Status.APPROVED,
        expiry_date__gte=today,
        expiry_date__lte=today + timedelta(days=60),
    ).order_by("expiry_date")
    for document in documents:
        events.append(
            {
                "type": IncubateePortalNotification.Type.DOCUMENT_EXPIRY,
                "title": (
                    f"{document.get_category_display()} business document "
                    "approaching expiry"
                ),
                "message": f"Expiry date: {document.expiry_date:%j %B %Y}.",
                "date": document.expiry_date,
                "read": False,
            }
        )

    upcoming = business.mentoring_sessions.filter(
        session_date__gte=today
    ).order_by("session_date")
    for session in upcoming:
        mentor_name = session.mentor.name if session.mentor_id else "your mentor"
        events.append(
            {
                "type": IncubateePortalNotification.Type.MENTORSHIP,
                "title": f"Mentorship session scheduled with {mentor_name}",
                "message": f"Session date: {session.session_date:%j %B %Y}.",
                "date": session.session_date,
                "read": False,
            }
        )

    stored = list(
        IncubateePortalNotification.objects.filter(
            user=user, incubatee=business
        ).order_by("-created_at")
    )
    for notification in stored:
        events.append(
            {
                "id": notification.pk,
                "type": notification.notification_type,
                "title": notification.title,
                "message": notification.message,
                "date": notification.created_at,
                "read": notification.read_at is not None,
                "email_error": notification.email_error,
            }
        )
    return sorted(events, key=lambda event: event["date"], reverse=True)


@login_required
def notification_centre(request, incubatee_id):
    business = _portal_business(request, incubatee_id)
    events = _notification_events(business, request.user)
    return render(
        request,
        "incubation/portal/notifications.html",
        {
            "business": business,
            "notifications": events,
            "unread_count": sum(not event["read"] for event in events),
        },
    )


@login_required
@require_POST
def notification_mark_read(request, incubatee_id, notification_id):
    business = _portal_business(request, incubatee_id)
    notification = get_object_or_404(
        IncubateePortalNotification,
        pk=notification_id,
        user=request.user,
        incubatee=business,
    )
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=("read_at",))
    return redirect(
        "incubation:notification_centre", incubatee_id=business.pk
    )
