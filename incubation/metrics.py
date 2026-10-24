from decimal import Decimal

from django.db.models import Sum

from incubation.models import (
    IncubateeMetricObservation,
    IncubateeFollowUp,
    MentoringSession,
    MonthlyCheckIn,
    ProgrammeCostObservation,
)

MONTHLY_METRICS = (
    IncubateeMetricObservation.Metric.REVENUE_ZAR,
    IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
    IncubateeMetricObservation.Metric.NEW_CUSTOMERS,
    IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES,
    IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES,
    IncubateeMetricObservation.Metric.JOBS_CREATED,
    IncubateeMetricObservation.Metric.JOBS_LOST,
    IncubateeMetricObservation.Metric.FUNDING_APPLIED_ZAR,
    IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
    IncubateeMetricObservation.Metric.BIGGEST_WIN,
    IncubateeMetricObservation.Metric.BIGGEST_CHALLENGE,
    IncubateeMetricObservation.Metric.CHALLENGE_CATEGORY,
    IncubateeMetricObservation.Metric.SUPPORT_NEEDED,
    IncubateeMetricObservation.Metric.CONFIDENCE_SCORE,
)


def incubatee_metrics(incubatee):
    baseline = incubatee.baseline_history.order_by("created_at", "pk").first()
    baseline_values = baseline.snapshot if baseline else {}
    baseline_date = baseline.created_at.date() if baseline else None
    baseline_revenue = baseline_values.get("monthly_average_revenue_zar")
    monthly = incubatee.metric_observations.filter(
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        period_start__gt=baseline_date if baseline_date else "0001-01-01",
    )
    latest_revenue = (
        monthly.filter(
            metric=IncubateeMetricObservation.Metric.REVENUE_ZAR,
            availability=IncubateeMetricObservation.Availability.RECORDED,
            decimal_value__isnull=False,
        )
        .order_by("-period_start")
        .values_list("decimal_value", flat=True)
        .first()
    )
    revenue_growth = None
    if baseline_revenue is not None and latest_revenue is not None:
        baseline_revenue = Decimal(str(baseline_revenue))
        if baseline_revenue != 0:
            revenue_growth = (
                (Decimal(latest_revenue) - baseline_revenue) / baseline_revenue * 100
            )

    observations = monthly.filter(
        availability=IncubateeMetricObservation.Availability.RECORDED,
    )
    jobs_query = observations.filter(
        metric=IncubateeMetricObservation.Metric.JOBS_CREATED
    )
    jobs_created = jobs_query.aggregate(total=Sum("integer_value"))["total"]
    funding_query = observations.filter(
        metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR
    )
    funding_received = funding_query.aggregate(total=Sum("decimal_value"))["total"]
    checkins = MonthlyCheckIn.objects.filter(incubatee=incubatee)
    submitted_checkins = checkins.filter(submitted_at__isnull=False).count()
    expected_checkins = checkins.count()
    completion_rate = (
        Decimal(submitted_checkins) / Decimal(expected_checkins) * 100
        if expected_checkins
        else None
    )
    mentoring_sessions = MentoringSession.objects.filter(incubatee=incubatee)
    mentoring_hours = mentoring_sessions.aggregate(total=Sum("mentoring_hours"))["total"]
    followups = IncubateeFollowUp.objects.filter(
        exit_record__incubatee=incubatee,
        months_after_exit=12,
        submitted_at__isnull=False,
    )
    followup_count = followups.count()
    survival_rate = (
        Decimal(followups.filter(business_status=IncubateeFollowUp.Status.OPERATING).count())
        / Decimal(followup_count)
        * 100
        if followup_count
        else None
    )
    cost_per_job = None
    if incubatee.programme_id:
        programme_costs = ProgrammeCostObservation.objects.filter(
            programme_id=incubatee.programme_id
        ).aggregate(total=Sum("amount_zar"))["total"]
        programme_jobs = IncubateeMetricObservation.objects.filter(
            incubatee__programme_id=incubatee.programme_id,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            metric=IncubateeMetricObservation.Metric.JOBS_CREATED,
            availability=IncubateeMetricObservation.Availability.RECORDED,
        ).aggregate(total=Sum("integer_value"))["total"]
        if programme_costs is not None and programme_jobs:
            cost_per_job = Decimal(programme_costs) / Decimal(programme_jobs)
    return {
        "revenue_growth_percent": revenue_growth,
        "jobs_created_since_baseline": jobs_created,
        "funding_received_recorded_zar": funding_received,
        "checkin_completion_rate": completion_rate,
        "mentoring_hours": mentoring_hours,
        "survival_rate_12_months": survival_rate,
        "cost_per_job_zar": cost_per_job,
    }


def data_completeness(checkin):
    observations = checkin.observations.filter(
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric__in=MONTHLY_METRICS,
    ).count()
    applicable_values = checkin.observations.filter(
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        metric__in=MONTHLY_METRICS,
        availability=IncubateeMetricObservation.Availability.RECORDED,
    ).count()
    not_applicable = observations - applicable_values
    expected = len(MONTHLY_METRICS)
    return {
        "score": round((observations / expected) * 100) if expected else 0,
        "recorded": applicable_values,
        "not_applicable": not_applicable,
        "missing": max(expected - observations, 0),
        "expected": expected,
    }
