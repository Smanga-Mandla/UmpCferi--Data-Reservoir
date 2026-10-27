from collections import defaultdict
from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from accounts.models import Role
from accounts.models import RoleGrant
from accounts.access import accessible_queryset
from audit.models import AuditEvent
from audit.services import log_event
from incubation.account_access import (
    can_manage_incubatee_accounts,
    manageable_account_businesses,
    manageable_businesses,
)
from incubation.models import (
    BusinessDocument,
    Incubatee,
    IncubateeAccount,
    IncubateeBusinessDevelopmentNeed,
    IncubateeBusinessDevelopmentNeedUpdate,
    IncubateeBaselineSnapshot,
    IncubateeMetricObservation,
    IncubateeMilestone,
    IncubateeSupportRequest,
    IncubateeSupportRequestUpdate,
    IncubateeProgressSubmission,
    MentoringSession,
    MonthlyCheckIn,
)
from incubation.portal_views import _business_for_portal
from incubation.report_access import report_accessible_businesses
from incubation.report_forms import StaffIncubateeMonitoringFilter
from interactions.models import Interaction

REPORT_METRICS = (
    IncubateeMetricObservation.Metric.REVENUE_ZAR,
    IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
    IncubateeMetricObservation.Metric.NEW_CUSTOMERS,
    IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES,
    IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES,
    IncubateeMetricObservation.Metric.FULL_TIME_MEN,
    IncubateeMetricObservation.Metric.FULL_TIME_WOMEN,
    IncubateeMetricObservation.Metric.FULL_TIME_YOUTH,
    IncubateeMetricObservation.Metric.PART_TIME_MEN,
    IncubateeMetricObservation.Metric.PART_TIME_WOMEN,
    IncubateeMetricObservation.Metric.PART_TIME_YOUTH,
    IncubateeMetricObservation.Metric.JOBS_CREATED,
    IncubateeMetricObservation.Metric.JOBS_LOST,
    IncubateeMetricObservation.Metric.FUNDING_APPLIED_ZAR,
    IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
    IncubateeMetricObservation.Metric.FUNDING_SOURCE,
    IncubateeMetricObservation.Metric.BUSINESS_STAGE,
    IncubateeMetricObservation.Metric.SUPPORT_NEEDED,
)


def _report_dates(request):
    period_start = request.GET.get("period_start", "").strip()
    period_end = request.GET.get("period_end", "").strip()
    try:
        start_date = date.fromisoformat(period_start) if period_start else None
        end_date = date.fromisoformat(period_end) if period_end else None
    except ValueError as error:
        raise ValueError("Choose a valid reporting date range.") from error
    if start_date and end_date and end_date < start_date:
        raise ValueError("Report end date must not precede its start date.")
    return start_date, end_date, period_start, period_end


def _observation_value(observation):
    if observation.decimal_value is not None:
        return observation.decimal_value
    if observation.integer_value is not None:
        return observation.integer_value
    return observation.text_value


def _not_provided(value):
    return "Not provided" if value is None else value


def _initial_baseline(records):
    records = list(records)
    initial_sources = {
        "staff_incubatee_registration",
        "staff_existing_business_link",
        "initial_onboarding_baseline",
    }
    return next(
        (record for record in records if record.source in initial_sources),
        records[0] if records else None,
    )


def _date_joined_cferi(baseline, business):
    joined_at = baseline.get("date_joined_cferi")
    return (
        date.fromisoformat(str(joined_at)[:10])
        if joined_at
        else business.created_at.date()
    )


def _business_for_report(user, incubatee_id):
    if user.roles.filter(code=Role.Code.INCUBATEE).exists():
        return _business_for_portal(user, incubatee_id)
    return get_object_or_404(
        report_accessible_businesses(user).select_related(
            "programme", "cohort"
        ),
        pk=incubatee_id,
    )


@login_required
def incubatee_reports(request, incubatee_id):
    business = _business_for_report(request.user, incubatee_id)
    try:
        start_date, end_date, period_start, period_end = _report_dates(request)
    except ValueError as error:
        messages.error(request, str(error))
        start_date = end_date = None
        period_start = period_end = ""

    observations = IncubateeMetricObservation.objects.filter(
        incubatee=business,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric__in=REPORT_METRICS,
        availability=IncubateeMetricObservation.Availability.RECORDED,
    )
    if start_date:
        observations = observations.filter(period_start__gte=start_date.replace(day=1))
    if end_date:
        observations = observations.filter(period_start__lte=end_date.replace(day=1))
    observations = observations.order_by("period_start", "metric")
    rows_by_period = defaultdict(dict)
    for observation in observations:
        rows_by_period[observation.period_start][observation.metric] = _observation_value(
            observation
        )
    trend_rows = [
        {"period": period, "values": values}
        for period, values in sorted(rows_by_period.items())
    ]
    latest_values = {}
    latest_observations = IncubateeMetricObservation.objects.filter(
        incubatee=business,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric__in=REPORT_METRICS,
        availability=IncubateeMetricObservation.Availability.RECORDED,
    )
    if end_date:
        latest_observations = latest_observations.filter(
            period_start__lte=end_date.replace(day=1)
        )
    for observation in latest_observations.order_by(
        "period_start", "metric", "pk"
    ):
        latest_values[observation.metric] = _observation_value(observation)
    baseline_record = _initial_baseline(
        IncubateeBaselineSnapshot.objects.filter(incubatee=business).order_by(
            "created_at", "pk"
        )
    )
    baseline = baseline_record.snapshot if baseline_record else {}
    date_joined_cferi = _date_joined_cferi(baseline, business)
    baseline_metrics = {
        "business_stage": baseline.get("business_stage"),
        "business_registration_status": baseline.get("business_registration_status"),
        "customer_count": baseline.get("customer_count"),
        "monthly_average_revenue_zar": baseline.get("monthly_average_revenue_zar"),
        "full_time_employees": baseline.get("full_time_employees"),
        "part_time_employees": baseline.get("part_time_employees"),
        "funding_received_zar": baseline.get("funding_received_zar"),
        "support_needs": baseline.get("top_support_needs"),
    }
    for field in ("monthly_average_revenue_zar", "funding_received_zar"):
        if baseline_metrics[field] is not None:
            baseline_metrics[field] = Decimal(str(baseline_metrics[field]))
    registration_labels = dict(Incubatee.RegistrationStatus.choices)
    stage_labels = dict(Incubatee.BusinessStage.choices)
    baseline_metrics["business_stage_display"] = stage_labels.get(
        baseline_metrics["business_stage"], baseline_metrics["business_stage"]
    )
    baseline_metrics["business_registration_status_display"] = registration_labels.get(
        baseline_metrics["business_registration_status"],
        baseline_metrics["business_registration_status"],
    )
    current_stage = latest_values.get(IncubateeMetricObservation.Metric.BUSINESS_STAGE)
    latest_values["business_stage_display"] = stage_labels.get(
        current_stage or business.business_stage, current_stage or business.business_stage
    )
    if "full_time_employees" in latest_values and "part_time_employees" in latest_values:
        latest_values["total_employees"] = (
            latest_values["full_time_employees"] + latest_values["part_time_employees"]
        )
    baseline_total_employees = None
    if (
        baseline_metrics["full_time_employees"] is not None
        and baseline_metrics["part_time_employees"] is not None
    ):
        baseline_total_employees = (
            baseline_metrics["full_time_employees"]
            + baseline_metrics["part_time_employees"]
        )
    monthly_funding = IncubateeMetricObservation.objects.filter(
        incubatee=business,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
        availability=IncubateeMetricObservation.Availability.RECORDED,
        decimal_value__isnull=False,
    )
    if end_date:
        monthly_funding = monthly_funding.filter(
            period_start__lte=end_date.replace(day=1)
        )
    monthly_funding_total = monthly_funding.aggregate(total=Sum("decimal_value"))[
        "total"
    ]
    baseline_funding = baseline_metrics["funding_received_zar"]
    if baseline_funding is not None:
        baseline_funding = Decimal(str(baseline_funding))
        baseline_metrics["funding_received_zar"] = baseline_funding
    cumulative_funding = (
        (monthly_funding_total or 0) + (baseline_funding or 0)
        if monthly_funding_total is not None or baseline_funding is not None
        else None
    )
    category_links = business.account_links.select_related("account")
    if request.user.roles.filter(code=Role.Code.INCUBATEE).exists():
        category_links = category_links.filter(account__user=request.user)
    categories = list(
        category_links.values_list("account__entrepreneur_category", flat=True).distinct()
    )
    challenges = business.business_challenges.prefetch_related("reviews")
    milestones = business.milestones.all()
    support_requests = business.support_requests.select_related(
        "requested_by", "assigned_to"
    ).prefetch_related("updates__assigned_to", "updates__updated_by")
    development_needs = business.business_development_needs.select_related(
        "requested_by", "assigned_to"
    ).prefetch_related("updates__assigned_to", "updates__updated_by")
    mentoring_sessions = business.mentoring_sessions.select_related("mentor")
    interactions = Interaction.objects.filter(
        incubatees=business,
        is_deleted=False,
    ).annotate(linked_business_count=Count("incubatees", distinct=True)).filter(
        linked_business_count=1
    )
    is_incubatee_user = request.user.roles.filter(code=Role.Code.INCUBATEE).exists()
    if is_incubatee_user:
        interactions = interactions.filter(sensitivity__in=("public", "internal"))
    else:
        interactions = accessible_queryset(
            request.user,
            RoleGrant.Resource.INTERACTIONS,
            RoleGrant.Action.READ,
            interactions,
        )
    interactions = interactions.order_by("-occurred_at")
    progress_history = MonthlyCheckIn.objects.filter(
        incubatee=business, submitted_at__isnull=False
    ).select_related("submitted_by").prefetch_related("observations", "portal_submission")
    progress_submissions = IncubateeProgressSubmission.objects.filter(
        checkin__incubatee=business
    ).select_related("checkin", "submitted_by")
    if start_date:
        challenges = challenges.filter(reported_at__date__gte=start_date)
        milestones = milestones.filter(milestone_date__gte=start_date)
        support_requests = support_requests.filter(submitted_at__date__gte=start_date)
        development_needs = development_needs.filter(identified_at__gte=start_date)
        mentoring_sessions = mentoring_sessions.filter(session_date__gte=start_date)
        interactions = interactions.filter(occurred_at__date__gte=start_date)
        progress_history = progress_history.filter(
            period_start__gte=start_date.replace(day=1)
        )
        progress_submissions = progress_submissions.filter(
            checkin__period_start__gte=start_date.replace(day=1)
        )
    if end_date:
        challenges = challenges.filter(reported_at__date__lte=end_date)
        milestones = milestones.filter(milestone_date__lte=end_date)
        support_requests = support_requests.filter(submitted_at__date__lte=end_date)
        development_needs = development_needs.filter(identified_at__lte=end_date)
        mentoring_sessions = mentoring_sessions.filter(session_date__lte=end_date)
        interactions = interactions.filter(occurred_at__date__lte=end_date)
        progress_history = progress_history.filter(
            period_start__lte=end_date.replace(day=1)
        )
        progress_submissions = progress_submissions.filter(
            checkin__period_start__lte=end_date.replace(day=1)
        )
    progress_by_checkin = {
        submission.checkin_id: submission for submission in progress_submissions
    }
    progress_detail_rows = []
    for checkin in progress_history:
        metric_values = {
            observation.metric: _observation_value(observation)
            for observation in checkin.observations.all()
            if observation.availability
            == IncubateeMetricObservation.Availability.RECORDED
        }
        progress_detail_rows.append(
            {
                "checkin": checkin,
                "metrics": metric_values,
                "submission": progress_by_checkin.get(checkin.pk),
            }
        )
    initial_support_needs = baseline_metrics["support_needs"]
    if isinstance(initial_support_needs, (list, tuple)):
        initial_support_needs = ", ".join(str(item) for item in initial_support_needs)
    elif initial_support_needs is None:
        initial_support_needs = None
    baseline_rows = [
        ("Date joined CFERI", date_joined_cferi),
        ("Initial business stage", baseline_metrics["business_stage_display"]),
        (
            "Initial registration status",
            baseline_metrics["business_registration_status_display"],
        ),
        ("Initial customer count", baseline_metrics["customer_count"]),
        ("Initial monthly revenue (ZAR)", baseline_metrics["monthly_average_revenue_zar"]),
        ("Initial full-time employees", baseline_metrics["full_time_employees"]),
        ("Initial part-time employees", baseline_metrics["part_time_employees"]),
        ("Initial total employees", baseline_total_employees),
        ("Initial funding received (ZAR)", baseline_funding),
        ("Initial support needs", initial_support_needs),
    ]
    latest_total_employees = latest_values.get("total_employees")
    support_updates = IncubateeBusinessDevelopmentNeedUpdate.objects.filter(
        need__incubatee=business
    ).select_related("need", "updated_by", "assigned_to", "mentoring_session", "interaction")
    support_request_updates = IncubateeSupportRequestUpdate.objects.filter(
        support_request__incubatee=business
    ).select_related("support_request", "updated_by", "assigned_to")
    if start_date:
        support_updates = support_updates.filter(created_at__date__gte=start_date)
        support_request_updates = support_request_updates.filter(
            created_at__date__gte=start_date
        )
    if end_date:
        support_updates = support_updates.filter(created_at__date__lte=end_date)
        support_request_updates = support_request_updates.filter(
            created_at__date__lte=end_date
        )
    account_category_labels = dict(IncubateeAccount.EntrepreneurCategory.choices)
    account_categories = [
        account_category_labels.get(category, category) for category in categories
    ]
    return render(
        request,
        "incubation/portal/my_reports.html",
        {
            "business": business,
            "date_joined_cferi": date_joined_cferi,
            "trend_rows": trend_rows,
            "latest_values": latest_values,
            "baseline_rows": baseline_rows,
            "baseline_metrics": baseline_metrics,
            "initial_support_needs": initial_support_needs,
            "baseline_total_employees": baseline_total_employees,
            "latest_total_employees": latest_total_employees,
            "current_business_stage": latest_values["business_stage_display"],
            "monthly_funding_total": monthly_funding_total,
            "cumulative_funding": cumulative_funding,
            "cumulative_funding_partial": (
                baseline_funding is None and monthly_funding_total is not None
            ),
            "account_categories": account_categories,
            "is_incubatee_user": is_incubatee_user,
            "chart_labels": [row["period"].strftime("%b %Y") for row in trend_rows],
            "revenue_values": [
                float(value) if value is not None else None
                for value in (
                    row["values"].get(IncubateeMetricObservation.Metric.REVENUE_ZAR)
                    for row in trend_rows
                )
            ],
            "customer_values": [
                row["values"].get(IncubateeMetricObservation.Metric.CUSTOMER_COUNT)
                for row in trend_rows
            ],
            "full_time_values": [
                row["values"].get(IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES)
                for row in trend_rows
            ],
            "part_time_values": [
                row["values"].get(IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES)
                for row in trend_rows
            ],
            "funding_values": [
                float(value) if value is not None else None
                for value in (
                    row["values"].get(
                        IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR
                    )
                    for row in trend_rows
                )
            ],
            "chart_customer_values": [
                row["values"].get(IncubateeMetricObservation.Metric.CUSTOMER_COUNT)
                for row in trend_rows
            ],
            "chart_employment_values": [
                (
                    row["values"].get(
                        IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES
                    )
                    + row["values"].get(
                        IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES
                    )
                    if row["values"].get(
                        IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES
                    ) is not None
                    and row["values"].get(
                        IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES
                    ) is not None
                    else None
                )
                for row in trend_rows
            ],
            "milestones": milestones,
            "challenges": challenges,
            "progress_history": progress_history,
            "progress_detail_rows": progress_detail_rows,
            "progress_submissions": progress_submissions,
            "support_requests": support_requests,
            "support_request_updates": support_request_updates,
            "development_needs": development_needs,
            "support_updates": support_updates,
            "mentoring_sessions": mentoring_sessions,
            "support_interactions": interactions,
            "period_start": period_start,
            "period_end": period_end,
        },
    )


@login_required
@require_GET
def incubatee_report_export(request, incubatee_id, format_name):
    if format_name not in {"xlsx", "pdf"}:
        raise PermissionDenied
    business = _business_for_report(request.user, incubatee_id)
    try:
        start_date, end_date, _period_start, _period_end = _report_dates(request)
    except ValueError as error:
        raise PermissionDenied(str(error)) from error
    observations = IncubateeMetricObservation.objects.filter(
        incubatee=business,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric__in=REPORT_METRICS,
        availability=IncubateeMetricObservation.Availability.RECORDED,
    ).order_by("period_start", "metric")
    if start_date:
        observations = observations.filter(
            period_start__gte=start_date.replace(day=1)
        )
    if end_date:
        observations = observations.filter(period_start__lte=end_date.replace(day=1))
    data = []
    for observation in observations:
        value = _observation_value(observation)
        data.append(
            [
                observation.period_start.strftime("%B %Y"),
                observation.get_metric_display(),
                value,
            ]
        )
    milestone_records = business.milestones.all()
    challenge_records = business.business_challenges.all()
    progress_records = MonthlyCheckIn.objects.filter(
        incubatee=business,
        submitted_at__isnull=False,
    ).select_related("submitted_by").prefetch_related("observations", "portal_submission")
    support_requests = business.support_requests.select_related(
        "requested_by", "assigned_to"
    ).prefetch_related("updates")
    development_needs = business.business_development_needs.select_related(
        "requested_by", "assigned_to"
    ).prefetch_related("updates")
    mentoring_sessions = business.mentoring_sessions.select_related("mentor")
    support_interactions = Interaction.objects.filter(
        incubatees=business,
        is_deleted=False,
    ).annotate(linked_business_count=Count("incubatees", distinct=True)).filter(
        linked_business_count=1
    )
    is_incubatee_user = request.user.roles.filter(code=Role.Code.INCUBATEE).exists()
    if is_incubatee_user:
        support_interactions = support_interactions.filter(
            sensitivity__in=("public", "internal")
        )
    else:
        support_interactions = accessible_queryset(
            request.user,
            RoleGrant.Resource.INTERACTIONS,
            RoleGrant.Action.READ,
            support_interactions,
        )
    if start_date:
        milestone_records = milestone_records.filter(milestone_date__gte=start_date)
        challenge_records = challenge_records.filter(reported_at__date__gte=start_date)
        progress_records = progress_records.filter(
            period_start__gte=start_date.replace(day=1)
        )
        support_requests = support_requests.filter(submitted_at__date__gte=start_date)
        development_needs = development_needs.filter(identified_at__gte=start_date)
        mentoring_sessions = mentoring_sessions.filter(session_date__gte=start_date)
        support_interactions = support_interactions.filter(
            occurred_at__date__gte=start_date
        )
    if end_date:
        milestone_records = milestone_records.filter(milestone_date__lte=end_date)
        challenge_records = challenge_records.filter(reported_at__date__lte=end_date)
        progress_records = progress_records.filter(
            period_start__lte=end_date.replace(day=1)
        )
        support_requests = support_requests.filter(submitted_at__date__lte=end_date)
        development_needs = development_needs.filter(identified_at__lte=end_date)
        mentoring_sessions = mentoring_sessions.filter(session_date__lte=end_date)
        support_interactions = support_interactions.filter(
            occurred_at__date__lte=end_date
        )
    baseline_record = _initial_baseline(
        IncubateeBaselineSnapshot.objects.filter(incubatee=business).order_by(
            "created_at", "pk"
        )
    )
    baseline = baseline_record.snapshot if baseline_record else {}
    date_joined_cferi = _date_joined_cferi(baseline, business)
    for field in ("monthly_average_revenue_zar", "funding_received_zar"):
        if baseline.get(field) is not None:
            baseline[field] = Decimal(str(baseline[field]))
    baseline_employees = None
    if (
        baseline.get("full_time_employees") is not None
        and baseline.get("part_time_employees") is not None
    ):
        baseline_employees = (
            baseline["full_time_employees"] + baseline["part_time_employees"]
        )
    latest_observations = IncubateeMetricObservation.objects.filter(
        incubatee=business,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric__in=REPORT_METRICS,
        availability=IncubateeMetricObservation.Availability.RECORDED,
    )
    if end_date:
        latest_observations = latest_observations.filter(
            period_start__lte=end_date.replace(day=1)
        )
    latest_values = {}
    for observation in latest_observations.order_by("period_start", "metric", "pk"):
        latest_values[observation.metric] = _observation_value(observation)
    cumulative_observations = IncubateeMetricObservation.objects.filter(
        incubatee=business,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
        availability=IncubateeMetricObservation.Availability.RECORDED,
        decimal_value__isnull=False,
    )
    if end_date:
        cumulative_observations = cumulative_observations.filter(
            period_start__lte=end_date.replace(day=1)
        )
    monthly_funding = cumulative_observations.aggregate(total=Sum("decimal_value"))[
        "total"
    ]
    baseline_funding = baseline.get("funding_received_zar")
    if baseline_funding is not None:
        baseline_funding = Decimal(str(baseline_funding))
    cumulative_funding = (
        (monthly_funding or 0) + (baseline_funding or 0)
        if monthly_funding is not None or baseline_funding is not None
        else None
    )
    account_links = business.account_links.select_related("account")
    if request.user.roles.filter(code=Role.Code.INCUBATEE).exists():
        account_links = account_links.filter(account__user=request.user)
    category_labels = dict(IncubateeAccount.EntrepreneurCategory.choices)
    entrepreneur_categories = ", ".join(
        category_labels.get(category, category)
        for category in account_links.values_list(
            "account__entrepreneur_category", flat=True
        ).distinct()
    )
    latest_full_time = latest_values.get(
        IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES
    )
    latest_part_time = latest_values.get(
        IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES
    )
    latest_employees = (
        latest_full_time + latest_part_time
        if latest_full_time is not None and latest_part_time is not None
        else None
    )
    summary = [
        ["Business name", business.legal_name],
        ["Incubatee category", _not_provided(entrepreneur_categories or None)],
        ["Date joined CFERI", date_joined_cferi.isoformat()],
        ["Programme", business.programme.name if business.programme_id else "Not provided"],
        [
            "Initial business stage",
            dict(Incubatee.BusinessStage.choices).get(
                baseline.get("business_stage"), baseline.get("business_stage") or "Not provided"
            ),
        ],
        [
            "Current business stage",
            dict(Incubatee.BusinessStage.choices).get(
                latest_values.get("business_stage") or business.business_stage,
                latest_values.get("business_stage") or business.business_stage or "Not provided",
            ),
        ],
        [
            "Initial business registration status",
            dict(Incubatee.RegistrationStatus.choices).get(
                baseline.get("business_registration_status"),
                baseline.get("business_registration_status") or "Not provided",
            ),
        ],
        ["Current business registration status", business.get_business_registration_status_display()],
        ["Initial customers", _not_provided(baseline.get("customer_count"))],
        ["Latest customers", _not_provided(latest_values.get(IncubateeMetricObservation.Metric.CUSTOMER_COUNT))],
        ["Initial monthly revenue (ZAR)", _not_provided(baseline.get("monthly_average_revenue_zar"))],
        ["Latest monthly revenue (ZAR)", _not_provided(latest_values.get(IncubateeMetricObservation.Metric.REVENUE_ZAR))],
        ["Initial full-time employees", _not_provided(baseline.get("full_time_employees"))],
        ["Initial part-time employees", _not_provided(baseline.get("part_time_employees"))],
        ["Initial total employees", _not_provided(baseline_employees)],
        ["Latest full-time employees", _not_provided(latest_values.get(IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES))],
        ["Latest part-time employees", _not_provided(latest_values.get(IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES))],
        ["Latest total employees", _not_provided(latest_employees)],
        ["Sum of monthly funding receipts through period end (ZAR)", _not_provided(monthly_funding)],
        [
            (
                "Reported cumulative funding (ZAR)"
                if baseline_funding is not None
                else "Monthly funding receipts subtotal (ZAR)"
            ),
            _not_provided(cumulative_funding),
        ],
        ["Initial funding baseline", "Not provided" if baseline_funding is None else baseline_funding],
        [
            "Initial support needs",
            (
                ", ".join(str(item) for item in baseline["top_support_needs"])
                if isinstance(baseline.get("top_support_needs"), (list, tuple))
                else baseline.get("top_support_needs") or "Not provided"
            ),
        ],
    ]
    milestones = [
        [
            milestone.milestone_date.isoformat(),
            milestone.title or milestone.get_milestone_type_display(),
            milestone.description,
            milestone.review_status,
            milestone.original_filename,
        ]
        for milestone in milestone_records
    ]
    challenges = [
        [
            challenge.reported_at.date().isoformat(),
            challenge.get_category_display(),
            challenge.description,
            challenge.get_status_display(),
            challenge.review_status,
            challenge.evidence_original_filename,
        ]
        for challenge in challenge_records
    ]
    progress = [
        [
            checkin.period_start.strftime("%B %Y"),
            checkin.submitted_at.isoformat(),
            checkin.submitted_by.get_full_name() if checkin.submitted_by else "",
            "Submitted",
        ]
        for checkin in progress_records
    ]
    progress_detail = []
    for checkin in progress_records:
        metric_values = {
            observation.get_metric_display(): _observation_value(observation)
            for observation in checkin.observations.all()
            if observation.availability
            == IncubateeMetricObservation.Availability.RECORDED
        }
        submission = getattr(checkin, "portal_submission", None)
        progress_detail.append(
            [
                checkin.period_start.strftime("%B %Y"),
                metric_values.get("Revenue this month (ZAR)"),
                metric_values.get("Number of customers"),
                metric_values.get("Full-time employees"),
                metric_values.get("Part-time employees"),
                metric_values.get("Funding received (ZAR)"),
                metric_values.get("Funding source this month"),
                metric_values.get("Business stage this month"),
                submission.business_achievements if submission else "",
                submission.challenges_experienced if submission else "",
                metric_values.get("Support needed next month"),
            ]
        )
    support_requested = [
        [
            support.submitted_at.isoformat(),
            support.get_category_display(),
            support.description,
            support.get_status_display(),
            support.assigned_to.get_full_name() if support.assigned_to_id else "",
            support.evidence_original_filename,
        ]
        for support in support_requests
    ]
    recorded_needs = [
        [
            need.identified_at.isoformat(),
            need.get_category_display(),
            need.description,
            need.get_status_display(),
            need.assigned_to.get_full_name() if need.assigned_to_id else "",
            need.resolution_date.isoformat() if need.resolution_date else "",
            need.evidence_original_filename,
        ]
        for need in development_needs
    ]
    support_provided = []
    support_update_records = IncubateeBusinessDevelopmentNeedUpdate.objects.filter(
        need__incubatee=business
    ).select_related("need", "mentoring_session", "interaction")
    support_response_records = IncubateeSupportRequestUpdate.objects.filter(
        support_request__incubatee=business
    ).select_related("support_request")
    if start_date:
        support_update_records = support_update_records.filter(
            created_at__date__gte=start_date
        )
        support_response_records = support_response_records.filter(
            created_at__date__gte=start_date
        )
    if end_date:
        support_update_records = support_update_records.filter(
            created_at__date__lte=end_date
        )
        support_response_records = support_response_records.filter(
            created_at__date__lte=end_date
        )
    for update in support_update_records:
        if update.support_provided or update.outcome:
            support_provided.append(
                [
                    update.created_at.isoformat(),
                    update.need.get_category_display(),
                    update.support_provided,
                    update.outcome,
                    update.get_status_display(),
                    (
                        update.mentoring_session.session_date.isoformat()
                        if update.mentoring_session_id
                        else ""
                    ),
                    (
                        update.interaction.get_interaction_type_display()
                        if update.interaction_id
                        else ""
                    ),
                    update.evidence_original_filename,
                ]
            )
    for update in support_response_records:
        if update.response:
            support_provided.append(
                [
                    update.created_at.isoformat(),
                    update.support_request.get_category_display(),
                    update.response,
                    "",
                    update.get_status_display() if update.status else "",
                    "",
                    "",
                    "",
                ]
            )
    mentoring = [
        [
            session.session_date.isoformat(),
            str(session.mentor) if session.mentor_id else "",
            str(session.mentoring_hours),
            session.topics,
        ]
        for session in mentoring_sessions
    ]
    interactions = [
        [
            interaction.occurred_at.isoformat(),
            interaction.get_interaction_type_display(),
            interaction.topics,
            "" if is_incubatee_user else interaction.outcome,
        ]
        for interaction in support_interactions.order_by("-occurred_at")
    ]
    sheets = [
        ("Business summary", ["Measure", "Value"], summary),
        ("Performance", ["Reporting period", "Measure", "Value"], data),
        ("Milestones", ["Date", "Milestone", "Description", "Review status", "Evidence"], milestones),
        ("Challenges", ["Reported", "Category", "Description", "Status", "Review", "Evidence"], challenges),
        ("Progress history", ["Period", "Submitted at", "Submitted by", "Status"], progress),
        (
            "Monthly progress details",
            [
                "Period", "Revenue ZAR", "Customers", "Full-time employees",
                "Part-time employees", "Funding received ZAR", "Funding source",
                "Business stage", "Achievements", "Challenges", "Support needed",
            ],
            progress_detail,
        ),
        ("Support requested", ["Submitted", "Category", "Description", "Status", "Assigned staff", "Evidence"], support_requested),
        ("Business support needs", ["Identified", "Category", "Description", "Status", "Assigned staff", "Resolved", "Evidence"], recorded_needs),
        ("Support provided", ["Date", "Need/request", "Assistance", "Outcome", "Status", "Mentoring session", "Interaction", "Evidence"], support_provided),
        ("Mentoring sessions", ["Date", "Mentor", "Hours", "Topics"], mentoring),
        ("Other interactions", ["Date", "Type", "Topics", "Outcome"], interactions),
    ]
    from reporting.views import _pdf_response, _workbook_response

    if format_name == "xlsx":
        response = _workbook_response(
            "my-business-report.xlsx",
            [
                (name, headers, rows)
                for name, headers, rows in sheets
            ],
        )
    else:
        pdf_rows = [
            ["Section", "Date / Period", "Measure / Category", "Details", "Status"]
        ]
        pdf_rows.extend(
            [
                ["Business summary", "", row[0], _not_provided(row[1]), "Recorded"]
                for row in summary
            ]
        )
        pdf_rows.extend(
            [["Performance", row[0], row[1], row[2], "Recorded"] for row in data]
        )
        pdf_rows.extend(
            [["Milestone", row[0], row[1], row[2], row[3]] for row in milestones]
        )
        pdf_rows.extend(
            [
                [
                    "Challenge",
                    row[0],
                    row[1],
                    row[2] + ("; Evidence: " + row[5] if row[5] else ""),
                    f"{row[3]} / {row[4]}",
                ]
                for row in challenges
            ]
        )
        pdf_rows.extend(
            [["Progress", row[0], "Submission", row[1], row[3]] for row in progress]
        )
        progress_headers = [
            "Revenue (ZAR)",
            "Customers",
            "Full-time employees",
            "Part-time employees",
            "Funding received (ZAR)",
            "Funding source",
            "Business stage",
            "Achievements",
            "Challenges",
            "Support needed",
        ]
        pdf_rows.extend(
            [
                [
                    "Monthly progress details",
                    row[0],
                    "Metrics and narrative",
                    "; ".join(
                        f"{label}: {_not_provided(value)}"
                        for label, value in zip(progress_headers, row[1:])
                    ),
                    "Submitted",
                ]
                for row in progress_detail
            ]
        )
        pdf_rows.extend(
            [["Support requested", row[0], row[1], row[2] + ("; Evidence: " + row[5] if row[5] else ""), row[3]]
             for row in support_requested]
        )
        pdf_rows.extend(
            [["Business support need", row[0], row[1], row[2], row[3]]
             for row in recorded_needs]
        )
        pdf_rows.extend(
            [["Support provided", row[0], row[1], row[2] + ("; " + row[3] if row[3] else ""), row[4]]
             for row in support_provided]
        )
        pdf_rows.extend(
            [["Mentoring session", row[0], "Mentor / topics", "; ".join(value for value in row[1:] if value), "Recorded"]
             for row in mentoring]
        )
        pdf_rows.extend(
            [["Other interaction", row[0], row[1], row[2] + ("; " + row[3] if row[3] else ""), "Recorded"]
             for row in interactions]
        )
        response = _pdf_response(
            "my-business-report.pdf",
            f"UMP CFERI My Reports — {business.legal_name}",
            pdf_rows,
        )
    log_event(
        actor=request.user,
        action=AuditEvent.Action.EXPORT,
        target=business,
        resource="incubation.incubatee_report",
        metadata={"format": format_name, "business_id": str(business.pk)},
        request=request,
    )
    return response


def staff_monitoring_context(request):
    if not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    form = StaffIncubateeMonitoringFilter(
        request.GET,
        user=request.user,
    )
    businesses = manageable_businesses(request.user)
    if form.is_valid():
        data = form.cleaned_data
        if data["business"]:
            businesses = businesses.filter(pk=data["business"].pk)
        if data["programme"]:
            businesses = businesses.filter(programme=data["programme"])
        if data["cohort"]:
            businesses = businesses.filter(cohort=data["cohort"])
        if data["status"]:
            businesses = businesses.filter(status=data["status"])
        if data["sector"]:
            businesses = businesses.filter(sub_sector=data["sector"])
        if data["province"]:
            businesses = businesses.filter(province=data["province"])
        if data["business_stage"]:
            businesses = businesses.filter(business_stage=data["business_stage"])
        if data["entrepreneur_category"]:
            businesses = businesses.filter(
                account_links__account__entrepreneur_category=data[
                    "entrepreneur_category"
                ]
            ).distinct()
        period = data["reporting_period"]
    else:
        businesses = businesses.none()
        period = None
    accounts = IncubateeAccount.objects.filter(
        business_links__incubatee__in=businesses
    ).distinct()
    scoped_business_ids = businesses.values("pk")
    checkins = MonthlyCheckIn.objects.filter(incubatee__in=businesses)
    submissions = MonthlyCheckIn.objects.filter(
        incubatee__in=businesses,
        submitted_at__isnull=False,
    ).select_related("incubatee", "submitted_by")
    if period:
        checkins = checkins.filter(period_start=period)
        submissions = submissions.filter(period_start=period)
    outstanding = checkins.filter(
        submitted_at__isnull=True,
        due_date__isnull=False,
    ).select_related("incubatee").order_by("due_date")
    pending_documents = BusinessDocument.objects.filter(
        incubatee__in=businesses,
        status=BusinessDocument.Status.PENDING,
    ).select_related("incubatee", "uploaded_by")
    open_requests = IncubateeSupportRequest.objects.filter(
        incubatee__in=businesses,
        status__in=(
            IncubateeSupportRequest.Status.SUBMITTED,
            IncubateeSupportRequest.Status.UNDER_REVIEW,
            IncubateeSupportRequest.Status.IN_PROGRESS,
        ),
    ).select_related("incubatee", "requested_by", "assigned_to")
    development_needs = IncubateeBusinessDevelopmentNeed.objects.filter(
        incubatee__in=businesses,
        status__in=(
            IncubateeBusinessDevelopmentNeed.Status.OPEN,
            IncubateeBusinessDevelopmentNeed.Status.IN_PROGRESS,
        ),
    ).select_related("incubatee", "requested_by", "assigned_to")
    upcoming_sessions = MentoringSession.objects.filter(
        incubatee__in=businesses,
        session_date__gte=timezone.localdate(),
    ).select_related("incubatee", "mentor").order_by("session_date")
    if period:
        period_end = period.replace(day=monthrange(period.year, period.month)[1])
        pending_documents = pending_documents.filter(
            created_at__date__range=(period, period_end)
        )
        development_needs = development_needs.filter(
            identified_at__range=(period, period_end)
        )
        open_requests = open_requests.filter(
            submitted_at__date__range=(period, period_end)
        )
        upcoming_sessions = upcoming_sessions.filter(
            session_date__range=(period, period_end)
        )
    trend_metrics = (
        IncubateeMetricObservation.Metric.REVENUE_ZAR,
        IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
        IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES,
        IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES,
    )
    trend_observations = IncubateeMetricObservation.objects.filter(
        incubatee_id__in=scoped_business_ids,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric__in=trend_metrics,
        availability=IncubateeMetricObservation.Availability.RECORDED,
    )
    if period:
        trend_observations = trend_observations.filter(period_start__lte=period)
    latest_by_business_metric = {}
    for observation in trend_observations.order_by(
        "incubatee_id", "metric", "-period_start", "-pk"
    ):
        latest_by_business_metric.setdefault(
            (observation.incubatee_id, observation.metric),
            _observation_value(observation),
        )
    latest_customers = [
        value
        for (business_id, metric), value in latest_by_business_metric.items()
        if metric == IncubateeMetricObservation.Metric.CUSTOMER_COUNT
        and value is not None
    ]
    latest_revenues = [
        value
        for (business_id, metric), value in latest_by_business_metric.items()
        if metric == IncubateeMetricObservation.Metric.REVENUE_ZAR
        and value is not None
    ]
    latest_employment_changes = []
    baseline_records = IncubateeBaselineSnapshot.objects.filter(
        incubatee_id__in=scoped_business_ids
    ).order_by("incubatee_id", "created_at", "pk")
    baseline_rows = {}
    for baseline_record in baseline_records:
        baseline_rows.setdefault(baseline_record.incubatee_id, []).append(
            baseline_record
        )
    first_baselines = {
        business_id: _initial_baseline(rows).snapshot
        for business_id, rows in baseline_rows.items()
    }
    for business_id in businesses.values_list("pk", flat=True):
        baseline = first_baselines.get(business_id, {})
        baseline_ft = baseline.get("full_time_employees")
        baseline_pt = baseline.get("part_time_employees")
        latest_ft = latest_by_business_metric.get(
            (business_id, IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES)
        )
        latest_pt = latest_by_business_metric.get(
            (business_id, IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES)
        )
        if all(value is not None for value in (baseline_ft, baseline_pt, latest_ft, latest_pt)):
            latest_employment_changes.append(
                (int(latest_ft) + int(latest_pt)) - (int(baseline_ft) + int(baseline_pt))
            )
    funding_observations = IncubateeMetricObservation.objects.filter(
        incubatee_id__in=scoped_business_ids,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
        availability=IncubateeMetricObservation.Availability.RECORDED,
        decimal_value__isnull=False,
    )
    if period:
        funding_observations = funding_observations.filter(period_start=period)
    funding_total = funding_observations.aggregate(total=Sum("decimal_value"))[
        "total"
    ]
    milestone_records = IncubateeMilestone.objects.filter(
        incubatee_id__in=scoped_business_ids
    )
    all_support_requests = IncubateeSupportRequest.objects.filter(
        incubatee_id__in=scoped_business_ids
    )
    delivered_support_updates = IncubateeBusinessDevelopmentNeedUpdate.objects.filter(
        need__incubatee_id__in=scoped_business_ids
    ).exclude(support_provided="")
    support_responses = IncubateeSupportRequestUpdate.objects.filter(
        support_request__incubatee_id__in=scoped_business_ids
    ).exclude(response="")
    mentoring_records = MentoringSession.objects.filter(
        incubatee_id__in=scoped_business_ids
    )
    linked_interactions = Interaction.objects.filter(
        incubatees__in=businesses,
        is_deleted=False,
    ).annotate(
        linked_business_count=Count("incubatees", distinct=True)
    ).filter(linked_business_count=1)
    linked_interactions = accessible_queryset(
        request.user,
        RoleGrant.Resource.INTERACTIONS,
        RoleGrant.Action.READ,
        linked_interactions,
    )
    if period:
        milestone_records = milestone_records.filter(milestone_date__range=(period, period_end))
        all_support_requests = all_support_requests.filter(
            submitted_at__date__range=(period, period_end)
        )
        delivered_support_updates = delivered_support_updates.filter(
            created_at__date__range=(period, period_end)
        )
        support_responses = support_responses.filter(
            created_at__date__range=(period, period_end)
        )
        mentoring_records = mentoring_records.filter(
            session_date__range=(period, period_end)
        )
        linked_interactions = linked_interactions.filter(
            occurred_at__date__range=(period, period_end)
        )
    submitted_count = checkins.filter(submitted_at__isnull=False).count()
    total_checkin_count = checkins.count()
    completion_rate = (
        round(submitted_count / total_checkin_count * 100, 1)
        if total_checkin_count
        else None
    )
    category_counts = accounts.values("entrepreneur_category").annotate(
        total=Count("pk", distinct=True)
    )
    category_map = {row["entrepreneur_category"]: row["total"] for row in category_counts}
    context = {
        "monitoring_filter": form,
        "monitoring_incubatee_count": accounts.count(),
        "monitoring_business_count": businesses.count(),
        "monitoring_student_count": category_map.get(
            IncubateeAccount.EntrepreneurCategory.STUDENT_ENTREPRENEUR, 0
        ),
        "monitoring_external_count": category_map.get(
            IncubateeAccount.EntrepreneurCategory.EXTERNAL_SME, 0
        ),
        "monitoring_active_count": businesses.filter(
            status=Incubatee.Status.ACTIVE
        ).count(),
        "monitoring_idea_stage_count": businesses.filter(
            business_stage=Incubatee.BusinessStage.IDEA
        ).count(),
        "monitoring_registered_count": businesses.filter(
            business_registration_status=Incubatee.RegistrationStatus.REGISTERED
        ).count(),
        "monitoring_revenue_business_count": sum(
            1 for value in latest_revenues if value > 0
        ),
        "monitoring_total_customers": sum(latest_customers),
        "monitoring_customer_business_count": len(latest_customers),
        "monitoring_employment_change": sum(latest_employment_changes),
        "monitoring_employment_business_count": len(latest_employment_changes),
        "monitoring_funding_total": funding_total,
        "monitoring_milestone_count": milestone_records.count(),
        "monitoring_support_request_received_count": all_support_requests.count(),
        "monitoring_support_delivered_count": delivered_support_updates.count(),
        "monitoring_support_response_count": support_responses.count(),
        "monitoring_mentoring_session_count": mentoring_records.count(),
        "monitoring_linked_interaction_count": linked_interactions.count(),
        "monitoring_progress_completion_rate": completion_rate,
        "monitoring_progress_submission_count": submitted_count,
        "monitoring_progress_expected_count": total_checkin_count,
        "monitoring_period": period,
        "monitoring_outstanding_count": outstanding.count(),
        "monitoring_outstanding": outstanding,
        "monitoring_recent_submissions": submissions.order_by(
            "-submitted_at"
        ),
        "monitoring_recent_count": submissions.count(),
        "monitoring_pending_document_count": pending_documents.count(),
        "monitoring_pending_documents": pending_documents[:8],
        "monitoring_open_support_count": open_requests.count(),
        "monitoring_open_support": open_requests.order_by("-submitted_at")[:8],
        "monitoring_development_need_count": development_needs.count(),
        "monitoring_development_needs": development_needs.order_by(
            "-identified_at", "-pk"
        )[:8],
        "monitoring_upcoming_sessions": upcoming_sessions[:8],
    }
    return context


def _require_cferi_staff(request):
    if (
        not request.user.roles.filter(code=Role.Code.CFERI_STAFF).exists()
        or not can_manage_incubatee_accounts(request.user)
    ):
        raise PermissionDenied


@login_required
def staff_incubatee_monitoring(request):
    _require_cferi_staff(request)
    context = staff_monitoring_context(request)
    return render(
        request,
        "incubation/account_management/monitoring.html",
        {**context, "page_title": "Incubatee Monitoring"},
    )


@login_required
def staff_outstanding_progress(request):
    _require_cferi_staff(request)
    context = staff_monitoring_context(request)
    return render(
        request,
        "incubation/account_management/progress_queue.html",
        {
            **context,
            "page_title": "Outstanding progress submissions",
            "queue_type": "outstanding",
            "progress_page": Paginator(context["monitoring_outstanding"], 25).get_page(
                request.GET.get("page")
            ),
        },
    )


@login_required
def staff_recent_progress(request):
    _require_cferi_staff(request)
    context = staff_monitoring_context(request)
    return render(
        request,
        "incubation/account_management/progress_queue.html",
        {
            **context,
            "page_title": "Recently submitted progress",
            "queue_type": "recent",
            "progress_page": Paginator(
                context["monitoring_recent_submissions"], 25
            ).get_page(request.GET.get("page")),
        },
    )


@login_required
def incubatee_account_profile(request, account_id):
    if not can_manage_incubatee_accounts(request.user):
        raise PermissionDenied
    account = get_object_or_404(
        IncubateeAccount.objects.select_related("user", "created_by").prefetch_related(
            "business_links__incubatee"
        ),
        pk=account_id,
    )
    authorized_businesses = manageable_account_businesses(request.user)
    links = [
        link
        for link in account.business_links.select_related("incubatee")
        if authorized_businesses.filter(pk=link.incubatee_id).exists()
    ]
    if not links:
        raise PermissionDenied("This account includes a business outside your authorized scope.")
    businesses = [link.incubatee for link in links]
    detailed_businesses = manageable_businesses(request.user)
    log_event(
        actor=request.user,
        action=AuditEvent.Action.SENSITIVE_VIEW,
        target=account,
        resource="incubation.incubatee_account_profile",
        metadata={"business_ids": [str(business.pk) for business in businesses]},
        request=request,
    )
    business_rows = []
    for business in businesses:
        limited_scope = not detailed_businesses.filter(pk=business.pk).exists()
        business_rows.append(
            {
                "business": business,
                "limited_scope": limited_scope,
                "progress": (
                    MonthlyCheckIn.objects.filter(
                        incubatee=business, submitted_at__isnull=False
                    ).order_by("-period_start")[:6]
                    if not limited_scope
                    else MonthlyCheckIn.objects.none()
                ),
                "documents": (
                    BusinessDocument.objects.filter(incubatee=business).order_by(
                        "-created_at"
                    )[:6]
                    if not limited_scope
                    else BusinessDocument.objects.none()
                ),
                "support_requests": (
                    IncubateeSupportRequest.objects.filter(
                        incubatee=business
                    ).order_by("-submitted_at")[:6]
                    if not limited_scope
                    else IncubateeSupportRequest.objects.none()
                ),
                "sessions": (
                    MentoringSession.objects.filter(incubatee=business)
                    .select_related("mentor")
                    .order_by("-session_date")[:6]
                    if not limited_scope
                    else MentoringSession.objects.none()
                ),
                "milestones": (
                    business.milestones.all()[:6]
                    if not limited_scope
                    else business.milestones.none()
                ),
            }
        )
    return render(
        request,
        "incubation/account_management/profile.html",
        {
            "account": account,
            "business_rows": business_rows,
        },
    )
