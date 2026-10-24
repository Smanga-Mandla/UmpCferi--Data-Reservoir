from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from accounts.access import accessible_queryset, has_access
from accounts.models import Role, RoleGrant
from documents.services import accessible_documents
from incubation.models import (
    Incubatee,
    IncubateeMetricObservation,
    MonthlyCheckIn,
)
from interactions.models import Interaction
from partners.models import MOU, Partner
from django.db.models import Q


PRIVACY_CELL_MINIMUM = 5
BASELINE_REPORT_FIELDS = (
    ("province", "Province"),
    ("sic_code", "Sector"),
    ("monthly_average_revenue_zar", "Baseline monthly revenue"),
    ("customer_count", "Baseline customer count"),
    ("full_time_employees", "Baseline full-time employees"),
)


def _role_codes(user):
    if user.is_superuser:
        return {Role.Code.SUPER_ADMIN}
    return set(user.roles.values_list("code", flat=True))


def is_board_aggregate(user):
    return bool(
        Role.Code.BOARD_MEMBER in _role_codes(user)
        and not (set(_role_codes(user)) - {Role.Code.BOARD_MEMBER})
    )


def _has_aggregate_grant(user, resource):
    return RoleGrant.objects.filter(
        role__code=Role.Code.BOARD_MEMBER,
        resource=resource,
        action=RoleGrant.Action.READ,
        scope=RoleGrant.Scope.AGGREGATE,
    ).exists()


def _report_incubatees(user):
    if is_board_aggregate(user):
        if not _has_aggregate_grant(user, RoleGrant.Resource.INCUBATEES):
            return Incubatee.objects.none()
        return Incubatee.objects.exclude(
            sensitivity=Incubatee.Sensitivity.RESTRICTED
        )
    if not has_access(user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ):
        return Incubatee.objects.none()
    report_scope = accessible_queryset(
        user,
        RoleGrant.Resource.REPORTS,
        RoleGrant.Action.READ,
        Incubatee.objects.all(),
    )
    return [
        incubatee
        for incubatee in report_scope.select_related("cohort", "programme")
        if has_access(
            user,
            RoleGrant.Resource.INCUBATEES,
            RoleGrant.Action.READ,
            incubatee,
        )
    ]


def _report_mous(user):
    if not has_access(user, RoleGrant.Resource.PARTNERS_MOUS, RoleGrant.Action.READ):
        return MOU.objects.none()
    partner_ids = accessible_queryset(
        user,
        RoleGrant.Resource.PARTNERS_MOUS,
        RoleGrant.Action.READ,
        Partner.objects.all(),
    ).values("pk")
    candidates = accessible_queryset(
        user,
        RoleGrant.Resource.PARTNERS_MOUS,
        RoleGrant.Action.READ,
        MOU.objects.filter(partner_id__in=partner_ids),
    ).filter(status=MOU.Status.ACTIVE)
    return [
        mou for mou in candidates
        if has_access(user, RoleGrant.Resource.PARTNERS_MOUS, RoleGrant.Action.READ, mou.partner)
    ]


def _report_documents(user):
    return accessible_documents(user)


def _report_checkins(user, incubatees, *, board_aggregate=False):
    if board_aggregate and _has_aggregate_grant(
        user, RoleGrant.Resource.CHECKINS_METRICS
    ):
        return MonthlyCheckIn.objects.filter(
            incubatee__in=incubatees,
            due_date__lt=timezone.localdate(),
            submitted_at__isnull=True,
        )
    allowed = [
        incubatee.pk
        for incubatee in incubatees
        if has_access(
            user,
            RoleGrant.Resource.CHECKINS_METRICS,
            RoleGrant.Action.READ,
            incubatee,
        )
    ]
    return MonthlyCheckIn.objects.filter(
        incubatee_id__in=allowed,
        due_date__lt=timezone.localdate(),
        submitted_at__isnull=True,
    )


def dashboard_data(user):
    incubatees = _report_incubatees(user)
    board_aggregate = is_board_aggregate(user)
    if isinstance(incubatees, list):
        status_counts = defaultdict(int)
        for incubatee in incubatees:
            status_counts[incubatee.status] += 1
    else:
        status_counts = {
            row["status"]: row["total"]
            for row in incubatees.values("status").annotate(total=Count("pk"))
        }
    if isinstance(incubatees, list):
        incubatee_ids = [record.pk for record in incubatees]
    else:
        incubatee_ids = list(incubatees.values_list("pk", flat=True))
    allowed_checkins = _report_checkins(
        user, incubatee_ids, board_aggregate=board_aggregate
    )
    documents = [
        document
        for document in _report_documents(user)
        if not document.archived
        and document.expiry_date is not None
        and document.expiry_date <= timezone.localdate() + timedelta(days=60)
    ]
    total_incubatees = sum(status_counts.values())
    status_rows = [
        {
            "value": key,
            "label": dict(Incubatee.Status.choices).get(key, key),
            "count": status_counts.get(key, 0)
            if not board_aggregate or status_counts.get(key, 0) >= PRIVACY_CELL_MINIMUM
            else None,
        }
        for key, _label in Incubatee.Status.choices
    ]
    return {
        "incubatee_total": (
            total_incubatees
            if not board_aggregate or total_incubatees >= PRIVACY_CELL_MINIMUM
            else None
        ),
        "incubatee_status_counts": status_rows,
        "active_mou_count": len(_report_mous(user)),
        "expiring_documents": documents,
        "overdue_checkins": (
            []
            if board_aggregate
            else allowed_checkins.select_related("incubatee")
        ),
        "overdue_checkin_count": (
            allowed_checkins.count()
            if not board_aggregate
            or allowed_checkins.count() >= PRIVACY_CELL_MINIMUM
            else None
        ),
        "can_view_business_data": bool(incubatee_ids),
        "board_aggregate": board_aggregate,
    }


def _observations(
    user, incubatees, *, date_from=None, date_to=None, board_aggregate=False
):
    if board_aggregate and not _has_aggregate_grant(
        user, RoleGrant.Resource.CHECKINS_METRICS
    ):
        return IncubateeMetricObservation.objects.none()
    if not board_aggregate and not has_access(
        user, RoleGrant.Resource.CHECKINS_METRICS, RoleGrant.Action.READ
    ):
        return IncubateeMetricObservation.objects.none()
    allowed_ids = (
        [
            incubatee.pk
            for incubatee in incubatees
            if incubatee.sensitivity != Incubatee.Sensitivity.RESTRICTED
        ]
        if board_aggregate
        else [
            incubatee.pk
            for incubatee in incubatees
            if has_access(
                user,
                RoleGrant.Resource.CHECKINS_METRICS,
                RoleGrant.Action.READ,
                incubatee,
            )
        ]
    )
    observations = IncubateeMetricObservation.objects.filter(
        incubatee_id__in=allowed_ids,
        period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
        availability=IncubateeMetricObservation.Availability.RECORDED,
    )
    if date_from:
        observations = observations.filter(period_start__gte=date_from)
    if date_to:
        observations = observations.filter(period_start__lte=date_to)
    return observations


def _revenue_growth_by_incubatee(incubatees, date_from, date_to):
    growth = {}
    for incubatee in incubatees:
        baseline = incubatee.baseline_history.order_by("created_at", "pk").first()
        if baseline is None:
            continue
        baseline_value = baseline.snapshot.get("monthly_average_revenue_zar")
        if baseline_value in (None, "", 0, "0", "0.00"):
            continue
        revenues = incubatee.metric_observations.filter(
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            metric=IncubateeMetricObservation.Metric.REVENUE_ZAR,
            availability=IncubateeMetricObservation.Availability.RECORDED,
            decimal_value__isnull=False,
        )
        if date_from:
            revenues = revenues.filter(period_start__gte=date_from)
        if date_to:
            revenues = revenues.filter(period_start__lte=date_to)
        latest = revenues.order_by("-period_start").values_list(
            "decimal_value", flat=True
        ).first()
        if latest is not None:
            base = Decimal(str(baseline_value))
            growth[incubatee.pk] = (Decimal(latest) - base) / base * 100
    return growth


def _group_label(incubatee, dimension):
    if dimension == "cohort":
        return incubatee.cohort.name if incubatee.cohort_id else "Unassigned"
    if dimension == "sector":
        return incubatee.sic_code or incubatee.sub_sector or "Not provided"
    if dimension == "province":
        return incubatee.province or "Not provided"
    return "All incubatees"


def _demographic_counts(user, incubatees):
    roles = _role_codes(user)
    if not (roles & {Role.Code.SUPER_ADMIN, Role.Code.ADMIN, Role.Code.BOARD_MEMBER}):
        return {"gender": [], "youth": []}
    if is_board_aggregate(user) and not _has_aggregate_grant(
        user, RoleGrant.Resource.CHECKINS_METRICS
    ):
        return {"gender": [], "youth": []}

    founder_counts = defaultdict(int)
    youth_counts = defaultdict(int)
    today = timezone.localdate()
    threshold = date(today.year - 35, today.month, min(today.day, 28))
    for incubatee in incubatees:
        links = incubatee.founders.filter(is_active=True).select_related("founder")
        for link in links:
            gender = link.founder.gender or "Not provided"
            founder_counts[gender] += 1
            dob = link.founder.date_of_birth
            youth_counts["Youth" if dob and dob > threshold else "Not classified"] += 1
    return {
        "gender": _suppress_small_cells(founder_counts),
        "youth": _suppress_small_cells(youth_counts),
    }


def _suppress_small_cells(counts):
    return [
        {"label": label, "count": count if count >= PRIVACY_CELL_MINIMUM else None}
        for label, count in sorted(counts.items())
    ]


def _aggregate_status(contributors, board_aggregate):
    if contributors == 0:
        return "missing"
    if board_aggregate and contributors < PRIVACY_CELL_MINIMUM:
        return "suppressed"
    return "recorded"


def impact_report(user, *, date_from=None, date_to=None):
    board_aggregate = is_board_aggregate(user)
    incubatees = _report_incubatees(user)
    if not isinstance(incubatees, list):
        incubatees = list(incubatees.select_related("cohort", "programme"))
    observations = _observations(
        user,
        incubatees,
        date_from=date_from,
        date_to=date_to,
        board_aggregate=board_aggregate,
    )
    incubatee_ids = [incubatee.pk for incubatee in incubatees]
    jobs = observations.filter(
        metric=IncubateeMetricObservation.Metric.JOBS_CREATED
    ).aggregate(total=Sum("integer_value"))["total"]
    funding = observations.filter(
        metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR
    ).aggregate(total=Sum("decimal_value"))["total"]
    jobs_contributors = observations.filter(
        metric=IncubateeMetricObservation.Metric.JOBS_CREATED
    ).values("incubatee_id").distinct().count()
    funding_contributors = observations.filter(
        metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR
    ).values("incubatee_id").distinct().count()
    jobs_by_period = {
        row["period_start"]: row
        for row in observations.filter(
            metric=IncubateeMetricObservation.Metric.JOBS_CREATED
        )
        .values("period_start")
        .annotate(total=Sum("integer_value"), contributors=Count("incubatee_id", distinct=True))
    }
    funding_by_period = {
        row["period_start"]: row
        for row in observations.filter(
            metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR
        )
        .values("period_start")
        .annotate(total=Sum("decimal_value"), contributors=Count("incubatee_id", distinct=True))
    }
    monthly_trend = []
    for period_start in sorted(set(jobs_by_period) | set(funding_by_period)):
        jobs_row = jobs_by_period.get(period_start, {})
        funding_row = funding_by_period.get(period_start, {})
        monthly_trend.append(
            {
                "period_start": period_start,
                "label": period_start.strftime("%b %Y"),
                "jobs_created": (
                    jobs_row.get("total")
                    if _aggregate_status(
                        jobs_row.get("contributors", 0), board_aggregate
                    )
                    == "recorded"
                    else None
                ),
                "funding_raised_zar": (
                    funding_row.get("total")
                    if _aggregate_status(
                        funding_row.get("contributors", 0), board_aggregate
                    )
                    == "recorded"
                    else None
                ),
                "jobs_status": _aggregate_status(
                    jobs_row.get("contributors", 0), board_aggregate
                ),
                "funding_status": _aggregate_status(
                    funding_row.get("contributors", 0), board_aggregate
                ),
            }
        )
    growth = _revenue_growth_by_incubatee(incubatees, date_from, date_to)
    breakdowns = {}
    for dimension in ("cohort", "sector", "province"):
        buckets = defaultdict(
            lambda: {
                "business_ids": set(),
                "jobs_incubatees": set(),
                "funding_incubatees": set(),
                "jobs": 0,
                "funding": Decimal("0"),
                "growth": [],
            }
        )
        for incubatee in incubatees:
            if not board_aggregate and not has_access(
                user,
                RoleGrant.Resource.CHECKINS_METRICS,
                RoleGrant.Action.READ,
                incubatee,
            ):
                continue
            label = _group_label(incubatee, dimension)
            bucket = buckets[label]
            bucket["business_ids"].add(incubatee.pk)
            bucket["growth"].append(growth[incubatee.pk]) if incubatee.pk in growth else None
        dimension_ids = [record.pk for record in incubatees]
        for observation in observations.filter(
            incubatee_id__in=dimension_ids,
            metric__in=(
                IncubateeMetricObservation.Metric.JOBS_CREATED,
                IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
            ),
        ).select_related("incubatee", "incubatee__cohort"):
            label = _group_label(observation.incubatee, dimension)
            if observation.metric == IncubateeMetricObservation.Metric.JOBS_CREATED:
                buckets[label]["jobs"] += observation.integer_value or 0
                buckets[label]["jobs_incubatees"].add(observation.incubatee_id)
            else:
                buckets[label]["funding"] += observation.decimal_value or Decimal("0")
                buckets[label]["funding_incubatees"].add(observation.incubatee_id)
        breakdowns[dimension] = [
            {
                "label": label,
                "businesses": (
                    len(bucket["business_ids"])
                    if not board_aggregate
                    or len(bucket["business_ids"]) >= PRIVACY_CELL_MINIMUM
                    else None
                ),
                "jobs_created": (
                    bucket["jobs"]
                    if _aggregate_status(
                        len(bucket["jobs_incubatees"]), board_aggregate
                    )
                    == "recorded"
                    else None
                ),
                "funding_raised_zar": (
                    bucket["funding"]
                    if _aggregate_status(
                        len(bucket["funding_incubatees"]), board_aggregate
                    )
                    == "recorded"
                    else None
                ),
                "jobs_status": _aggregate_status(
                    len(bucket["jobs_incubatees"]), board_aggregate
                ),
                "funding_status": _aggregate_status(
                    len(bucket["funding_incubatees"]), board_aggregate
                ),
                "revenue_growth_percent": (
                    sum(bucket["growth"], Decimal("0")) / len(bucket["growth"])
                    if bucket["growth"]
                    and (
                        not board_aggregate
                        or len(bucket["growth"]) >= PRIVACY_CELL_MINIMUM
                    )
                    else None
                ),
                "revenue_growth_status": _aggregate_status(
                    len(bucket["growth"]), board_aggregate
                ),
            }
            for label, bucket in sorted(buckets.items())
        ]

    demographic = _demographic_counts(user, incubatees)
    for dimension in ("gender", "youth"):
        breakdowns[dimension] = demographic[dimension]
    return {
        "date_from": date_from,
        "date_to": date_to,
        "business_count": (
            len(set(incubatee_ids))
            if not board_aggregate or len(set(incubatee_ids)) >= PRIVACY_CELL_MINIMUM
            else None
        ),
        "jobs_created": (
            jobs
            if _aggregate_status(jobs_contributors, board_aggregate) == "recorded"
            else None
        ),
        "funding_raised_zar": (
            funding
            if _aggregate_status(funding_contributors, board_aggregate) == "recorded"
            else None
        ),
        "jobs_status": _aggregate_status(jobs_contributors, board_aggregate),
        "funding_status": _aggregate_status(funding_contributors, board_aggregate),
        "revenue_growth_percent": (
            sum(growth.values(), Decimal("0")) / len(growth)
            if growth
            and (not board_aggregate or len(growth) >= PRIVACY_CELL_MINIMUM)
            else None
        ),
        "revenue_growth_businesses": (
            len(growth)
            if not board_aggregate or len(growth) >= PRIVACY_CELL_MINIMUM
            else None
        ),
        "revenue_growth_status": _aggregate_status(
            len(growth), board_aggregate
        ),
        "jobs_contributors": jobs_contributors,
        "funding_contributors": funding_contributors,
        "monthly_trend": monthly_trend,
        "breakdowns": breakdowns,
        "incubatees": incubatees,
        "growth_by_incubatee": growth,
        "privacy_minimum": PRIVACY_CELL_MINIMUM,
        "can_view_demographics": bool(demographic["gender"] or demographic["youth"]),
    }


def data_quality_rows(user):
    if is_board_aggregate(user):
        return []
    incubatees = _report_incubatees(user)
    if not isinstance(incubatees, list):
        incubatees = list(incubatees)
    visible_ids = [incubatee.pk for incubatee in incubatees]
    rows = []
    for incubatee in incubatees:
        missing = [
            label
            for field, label in BASELINE_REPORT_FIELDS
            if getattr(incubatee, field) in (None, "")
        ]
        duplicate_query = Q(legal_name__iexact=incubatee.legal_name)
        if incubatee.cipc_registration_number:
            duplicate_query |= Q(
                cipc_registration_number=incubatee.cipc_registration_number
            )
        possible_duplicate = Incubatee.objects.filter(
            pk__in=visible_ids,
            is_deleted=False,
        ).filter(duplicate_query).exclude(pk=incubatee.pk).exists()
        checkins = MonthlyCheckIn.objects.filter(incubatee=incubatee)
        latest = checkins.order_by("-period_start").first()
        completeness = latest.completeness_score if latest else None
        can_read_checkins = has_access(
            user,
            RoleGrant.Resource.CHECKINS_METRICS,
            RoleGrant.Action.READ,
            incubatee,
        )
        rows.append(
            {
                "incubatee": incubatee,
                "missing": missing,
                "possible_duplicate": possible_duplicate,
                "profile_completeness": round(
                    100 * (len(BASELINE_REPORT_FIELDS) - len(missing))
                    / len(BASELINE_REPORT_FIELDS)
                ),
                "overdue": bool(
                    can_read_checkins
                    and checkins.filter(
                        due_date__lt=timezone.localdate(),
                        submitted_at__isnull=True,
                    ).exists()
                ),
                "completeness": completeness if can_read_checkins else None,
            }
        )
    return rows
