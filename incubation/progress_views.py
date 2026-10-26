from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Role
from audit.models import AuditEvent
from audit.services import log_event
from incubation.account_access import can_manage_incubatee_accounts, manageable_businesses
from incubation.models import (
    Incubatee,
    IncubateeBusinessChallenge,
    IncubateeChallengeReview,
    IncubateeMetricObservation,
    IncubateeMilestone,
    IncubateeMilestoneReview,
    IncubateeProgressReview,
    IncubateeProgressSubmission,
    MonthlyCheckIn,
)
from incubation.progress_forms import (
    IncubateeChallengeForm,
    IncubateeMilestoneForm,
    IncubateeProgressForm,
    IncubateeReviewForm,
)
from incubation.portal_views import _business_for_portal, _require_incubatee

PROGRESS_METRICS = {
    "revenue_zar": IncubateeMetricObservation.Metric.REVENUE_ZAR,
    "customer_count": IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
    "full_time_employees": IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES,
    "part_time_employees": IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES,
    "full_time_men": IncubateeMetricObservation.Metric.FULL_TIME_MEN,
    "full_time_women": IncubateeMetricObservation.Metric.FULL_TIME_WOMEN,
    "full_time_youth": IncubateeMetricObservation.Metric.FULL_TIME_YOUTH,
    "part_time_men": IncubateeMetricObservation.Metric.PART_TIME_MEN,
    "part_time_women": IncubateeMetricObservation.Metric.PART_TIME_WOMEN,
    "part_time_youth": IncubateeMetricObservation.Metric.PART_TIME_YOUTH,
    "funding_received_zar": IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
}


def _incubatee_business(request, incubatee_id):
    _require_incubatee(request.user)
    return _business_for_portal(request.user, incubatee_id)


@login_required
def progress_history(request, incubatee_id):
    business = _incubatee_business(request, incubatee_id)
    portal_submissions = list(
        IncubateeProgressSubmission.objects.filter(checkin__incubatee=business)
        .select_related("checkin", "submitted_by")
        .prefetch_related("reviews")
    )
    portal_submissions_by_checkin = {
        submission.checkin_id: submission for submission in portal_submissions
    }
    checkins = list(
        MonthlyCheckIn.objects.filter(
            incubatee=business,
            submitted_at__isnull=False,
        ).select_related("submitted_by")
    )
    submission_ids = [checkin.pk for checkin in checkins]
    metrics_by_checkin = {}
    for observation in IncubateeMetricObservation.objects.filter(
        monthly_checkin_id__in=submission_ids
    ):
        metrics_by_checkin.setdefault(observation.monthly_checkin_id, {})[
            observation.metric
        ] = (
            observation.decimal_value
            if observation.decimal_value is not None
            else observation.integer_value
        )
    submissions = []
    for checkin in checkins:
        portal_submission = portal_submissions_by_checkin.get(checkin.pk)
        submissions.append(
            {
                "checkin": checkin,
                "metric_values": metrics_by_checkin.get(checkin.pk, {}),
                "business_achievements": (
                    portal_submission.business_achievements if portal_submission else ""
                ),
                "challenges_experienced": (
                    portal_submission.challenges_experienced if portal_submission else ""
                ),
                "additional_comments": (
                    portal_submission.additional_comments if portal_submission else ""
                ),
                "review_status_display": (
                    portal_submission.review_status_display
                    if portal_submission
                    else "Submitted"
                ),
                "review_note": portal_submission.review_note if portal_submission else "",
            }
        )
    trend = {}
    for observation in IncubateeMetricObservation.objects.filter(
        incubatee=business,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric__in=(
            IncubateeMetricObservation.Metric.REVENUE_ZAR,
            IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
            IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES,
            IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES,
        ),
        availability=IncubateeMetricObservation.Availability.RECORDED,
    ).order_by("period_start"):
        bucket = trend.setdefault(observation.period_start.isoformat(), {})
        bucket[observation.metric] = (
            float(observation.decimal_value)
            if observation.decimal_value is not None
            else observation.integer_value
        )
    chart_labels = [date.fromisoformat(period).strftime("%b %Y") for period in trend]
    chart_rows = list(trend.values())
    return render(
        request,
        "incubation/portal/progress_history.html",
        {
            "business": business,
            "submissions": submissions,
            "chart_labels": chart_labels,
            "revenue_values": [row.get("revenue_zar") for row in chart_rows],
            "customer_values": [row.get("customer_count") for row in chart_rows],
            "full_time_values": [row.get("full_time_employees") for row in chart_rows],
            "part_time_values": [row.get("part_time_employees") for row in chart_rows],
        },
    )


@login_required
def progress_submit(request, incubatee_id):
    business = _incubatee_business(request, incubatee_id)
    if request.method == "POST":
        form = IncubateeProgressForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                locked_business = Incubatee.objects.select_for_update().get(pk=business.pk)
                period = form.cleaned_data["period_start"]
                checkin = MonthlyCheckIn.objects.filter(
                    incubatee=locked_business,
                    period_start=period,
                ).first()
                if checkin and (
                    checkin.submitted_at or hasattr(checkin, "portal_submission")
                ):
                    form.add_error(
                        None,
                        "A progress submission already exists for this reporting period.",
                    )
                elif IncubateeMetricObservation.objects.filter(
                    incubatee=locked_business,
                    period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
                    period_start=period,
                    metric__in=PROGRESS_METRICS.values(),
                ).exists():
                    form.add_error(
                        None,
                        "This period already contains recorded metrics and cannot be overwritten.",
                    )
                else:
                    if checkin is None:
                        checkin = MonthlyCheckIn.objects.create(
                            incubatee=locked_business,
                            period_start=period,
                        )
                    for field, metric in PROGRESS_METRICS.items():
                        value = form.cleaned_data[field]
                        observation = IncubateeMetricObservation(
                            incubatee=locked_business,
                            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
                            period_start=period,
                            metric=metric,
                            source="incubatee_portal",
                            monthly_checkin=checkin,
                            recorded_by=request.user,
                        )
                        if metric in {
                            IncubateeMetricObservation.Metric.REVENUE_ZAR,
                            IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
                        }:
                            observation.decimal_value = value
                        else:
                            observation.integer_value = value
                        observation.save()
                    checkin.submitted_at = timezone.now()
                    checkin.submitted_by = request.user
                    checkin.save(
                        update_fields=("submitted_at", "submitted_by")
                    )
                    submission = IncubateeProgressSubmission.objects.create(
                        checkin=checkin,
                        submitted_by=request.user,
                        business_achievements=form.cleaned_data["business_achievements"],
                        challenges_experienced=form.cleaned_data["challenges_experienced"],
                        additional_comments=form.cleaned_data["additional_comments"],
                    )
                    log_event(
                        actor=request.user,
                        action=AuditEvent.Action.CREATE,
                        target=submission,
                        resource="incubation.progress_submission",
                        metadata={
                            "business_id": str(business.pk),
                            "period_start": period.isoformat(),
                        },
                        request=request,
                    )
                    messages.success(request, "Your monthly progress update was submitted.")
                    return redirect(
                        "incubation:progress_history", incubatee_id=business.pk
                    )
            if form.errors:
                return render(
                    request,
                    "incubation/portal/progress_form.html",
                    {"business": business, "form": form},
                )
    else:
        form = IncubateeProgressForm(initial={"reporting_year": timezone.localdate().year})
    return render(
        request,
        "incubation/portal/progress_form.html",
        {"business": business, "form": form},
    )


@login_required
def milestone_list(request, incubatee_id):
    business = _incubatee_business(request, incubatee_id)
    milestones = business.milestones.prefetch_related("reviews")
    return render(
        request,
        "incubation/portal/milestone_list.html",
        {
            "business": business,
            "milestones": milestones,
            "form": IncubateeMilestoneForm(),
        },
    )


@login_required
@require_POST
def milestone_submit(request, incubatee_id):
    business = _incubatee_business(request, incubatee_id)
    form = IncubateeMilestoneForm(request.POST, request.FILES)
    if form.is_valid():
        evidence = form.cleaned_data.get("evidence")
        with transaction.atomic():
            milestone = IncubateeMilestone(
                incubatee=business,
                title=form.cleaned_data["title"],
                milestone_type=form.cleaned_data["milestone_type"],
                milestone_date=form.cleaned_data["achieved_on"],
                description=form.cleaned_data["description"],
                source="incubatee_portal",
                recorded_by=request.user,
            )
            if evidence:
                milestone.evidence = evidence
                milestone.original_filename = form.safe_filename(evidence)
                milestone.content_type = form.cleaned_data["detected_content_type"]
                milestone.size_bytes = evidence.size
            milestone.save()
            log_event(
                actor=request.user,
                action=AuditEvent.Action.CREATE,
                target=milestone,
                resource="incubation.milestone",
                metadata={"business_id": str(business.pk)},
                request=request,
            )
        messages.success(request, "Your business milestone was recorded.")
        return redirect("incubation:milestone_list", incubatee_id=business.pk)
    return render(
        request,
        "incubation/portal/milestone_list.html",
        {"business": business, "milestones": business.milestones.all(), "form": form},
    )


@login_required
def challenge_list(request, incubatee_id):
    business = _incubatee_business(request, incubatee_id)
    return render(
        request,
        "incubation/portal/challenge_list.html",
        {
            "business": business,
            "challenges": business.business_challenges.prefetch_related("reviews"),
            "form": IncubateeChallengeForm(),
        },
    )


@login_required
@require_POST
def challenge_submit(request, incubatee_id):
    business = _incubatee_business(request, incubatee_id)
    form = IncubateeChallengeForm(request.POST, request.FILES)
    if form.is_valid():
        evidence = form.cleaned_data.get("evidence")
        with transaction.atomic():
            challenge = IncubateeBusinessChallenge(
                incubatee=business,
                category=form.cleaned_data["category"],
                description=form.cleaned_data["description"],
                reported_by=request.user,
            )
            if evidence:
                challenge.evidence = evidence
                challenge.evidence_original_filename = form.safe_filename(evidence)
                challenge.evidence_content_type = form.cleaned_data["detected_content_type"]
                challenge.evidence_size_bytes = evidence.size
            challenge.save()
            log_event(
                actor=request.user,
                action=AuditEvent.Action.CREATE,
                target=challenge,
                resource="incubation.business_challenge",
                metadata={"business_id": str(business.pk)},
                request=request,
            )
        messages.success(request, "Your business challenge was reported.")
        return redirect("incubation:challenge_list", incubatee_id=business.pk)
    return render(
        request,
        "incubation/portal/challenge_list.html",
        {
            "business": business,
            "challenges": business.business_challenges.all(),
            "form": form,
        },
    )


@login_required
def progress_review_list(request):
    if not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    scope = manageable_businesses(request.user)
    submissions = list(
        IncubateeProgressSubmission.objects.filter(checkin__incubatee__in=scope)
        .select_related("checkin__incubatee", "submitted_by")
        .prefetch_related("reviews")
    )
    observations_by_checkin = {}
    for observation in IncubateeMetricObservation.objects.filter(
        monthly_checkin_id__in=[submission.checkin_id for submission in submissions]
    ):
        observations_by_checkin.setdefault(observation.monthly_checkin_id, {})[
            observation.metric
        ] = (
            observation.decimal_value
            if observation.decimal_value is not None
            else observation.integer_value
        )
    for submission in submissions:
        submission.metric_values = observations_by_checkin.get(
            submission.checkin_id, {}
        )
    return render(
        request,
        "incubation/portal/progress_review_list.html",
        {
            "submissions": submissions,
            "milestones": IncubateeMilestone.objects.filter(
                incubatee__in=scope
            ).select_related("incubatee", "recorded_by").prefetch_related("reviews"),
            "challenges": IncubateeBusinessChallenge.objects.filter(
                incubatee__in=scope
            ).select_related("incubatee", "reported_by").prefetch_related("reviews"),
            "progress_review_choices": IncubateeProgressReview.Status.choices,
            "milestone_review_choices": IncubateeMilestoneReview.Status.choices,
            "challenge_review_choices": IncubateeChallengeReview.Status.choices,
        },
    )


@login_required
@require_POST
def progress_review(request, kind, record_id):
    if not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    scope = manageable_businesses(request.user)
    if kind == "progress":
        record = get_object_or_404(
            IncubateeProgressSubmission.objects.select_related("checkin__incubatee"),
            pk=record_id,
            checkin__incubatee__in=scope,
        )
        review_model = IncubateeProgressReview
        choices = IncubateeProgressReview.Status.choices
        target = record
        resource = "incubation.progress_review"
    elif kind == "milestone":
        record = get_object_or_404(
            IncubateeMilestone.objects.select_related("incubatee"),
            pk=record_id,
            incubatee__in=scope,
        )
        review_model = IncubateeMilestoneReview
        choices = IncubateeMilestoneReview.Status.choices
        target = record
        resource = "incubation.milestone_review"
    elif kind == "challenge":
        record = get_object_or_404(
            IncubateeBusinessChallenge.objects.select_related("incubatee"),
            pk=record_id,
            incubatee__in=scope,
        )
        review_model = IncubateeChallengeReview
        choices = IncubateeChallengeReview.Status.choices
        target = record
        resource = "incubation.challenge_review"
    else:
        raise PermissionDenied
    form = IncubateeReviewForm(request.POST, choices=choices)
    if not form.is_valid():
        messages.error(request, "Select a valid review status.")
        return redirect("incubation:progress_review_list")
    if kind == "progress" and record.submitted_by_id == request.user.pk:
        raise PermissionDenied("Reviewers cannot approve their own submissions.")
    review_status = form.cleaned_data["status"]
    with transaction.atomic():
        record = type(record).objects.select_for_update().get(pk=record.pk)
        if kind == "challenge":
            record.status = review_status
            record.save(update_fields=("status",))
        review = review_model.objects.create(
            **{
                {"progress": "submission", "milestone": "milestone", "challenge": "challenge"}[
                    kind
                ]: record,
                "status": review_status,
                "reviewed_by": request.user,
                "review_note": form.cleaned_data["review_note"],
            }
        )
        log_event(
            actor=request.user,
            action=AuditEvent.Action.UPDATE,
            target=target,
            resource=resource,
            metadata={
                "business_id": str(
                    record.checkin.incubatee_id
                    if kind == "progress"
                    else record.incubatee_id
                ),
                "review_id": review.pk,
                "status": review.status,
                "review_note": review.review_note,
            },
            request=request,
        )
    messages.success(request, "Review action recorded.")
    return redirect("incubation:progress_review_list")


@login_required
def progress_evidence_download(request, incubatee_id, kind, record_id):
    business = _business_for_portal(request.user, incubatee_id)
    if request.user.roles.filter(code=Role.Code.INCUBATEE).exists():
        _require_incubatee(request.user)
    elif not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    if kind == "milestone":
        record = get_object_or_404(
            IncubateeMilestone,
            pk=record_id,
            incubatee=business,
        )
        file = record.evidence
        filename = record.original_filename or "milestone-evidence"
    elif kind == "challenge":
        record = get_object_or_404(
            IncubateeBusinessChallenge,
            pk=record_id,
            incubatee=business,
        )
        file = record.evidence
        filename = record.evidence_original_filename or "challenge-evidence"
    else:
        raise PermissionDenied
    if not file:
        raise PermissionDenied
    log_event(
        actor=request.user,
        action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
        target=record,
        resource=f"incubation.{kind}_evidence",
        metadata={"business_id": str(business.pk)},
        request=request,
    )
    response = FileResponse(file.open("rb"), as_attachment=True, filename=filename)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
