from collections import defaultdict
from calendar import monthrange
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from accounts.models import Role
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
    IncubateeMetricObservation,
    IncubateeSupportRequest,
    MentoringSession,
    MonthlyCheckIn,
)
from incubation.progress_views import _incubatee_business
from incubation.report_forms import StaffIncubateeMonitoringFilter

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


@login_required
def incubatee_reports(request, incubatee_id):
    business = _incubatee_business(request, incubatee_id)
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
        rows_by_period[observation.period_start][observation.metric] = (
            observation.decimal_value
            if observation.decimal_value is not None
            else observation.integer_value
        )
    trend_rows = [
        {"period": period, "values": values}
        for period, values in sorted(rows_by_period.items())
    ]
    latest_values = {}
    for row in reversed(trend_rows):
        for metric, value in row["values"].items():
            latest_values.setdefault(metric, value)
    challenges = business.business_challenges.prefetch_related("reviews")
    milestones = business.milestones.all()
    progress_history = MonthlyCheckIn.objects.filter(
        incubatee=business, submitted_at__isnull=False
    ).select_related("submitted_by")
    if start_date:
        challenges = challenges.filter(reported_at__date__gte=start_date)
        milestones = milestones.filter(milestone_date__gte=start_date)
        progress_history = progress_history.filter(
            period_start__gte=start_date.replace(day=1)
        )
    if end_date:
        challenges = challenges.filter(reported_at__date__lte=end_date)
        milestones = milestones.filter(milestone_date__lte=end_date)
        progress_history = progress_history.filter(
            period_start__lte=end_date.replace(day=1)
        )
    return render(
        request,
        "incubation/portal/my_reports.html",
        {
            "business": business,
            "trend_rows": trend_rows,
            "latest_values": latest_values,
            "chart_labels": [row["period"].strftime("%b %Y") for row in trend_rows],
            "revenue_values": [
                row["values"].get(IncubateeMetricObservation.Metric.REVENUE_ZAR)
                for row in trend_rows
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
                row["values"].get(IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR)
                for row in trend_rows
            ],
            "milestones": milestones,
            "challenges": challenges,
            "progress_history": progress_history,
            "period_start": period_start,
            "period_end": period_end,
        },
    )


@login_required
@require_GET
def incubatee_report_export(request, incubatee_id, format_name):
    if format_name not in {"xlsx", "pdf"}:
        raise PermissionDenied
    business = _incubatee_business(request, incubatee_id)
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
        value = (
            observation.decimal_value
            if observation.decimal_value is not None
            else observation.integer_value
        )
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
    ).select_related("submitted_by")
    if start_date:
        milestone_records = milestone_records.filter(milestone_date__gte=start_date)
        challenge_records = challenge_records.filter(reported_at__date__gte=start_date)
        progress_records = progress_records.filter(
            period_start__gte=start_date.replace(day=1)
        )
    if end_date:
        milestone_records = milestone_records.filter(milestone_date__lte=end_date)
        challenge_records = challenge_records.filter(reported_at__date__lte=end_date)
        progress_records = progress_records.filter(
            period_start__lte=end_date.replace(day=1)
        )
    milestones = [
        [
            milestone.milestone_date.isoformat(),
            milestone.title or milestone.get_milestone_type_display(),
            milestone.description,
            milestone.review_status,
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
    sheets = [
        ("Performance", ["Reporting period", "Measure", "Value"], data),
        ("Milestones", ["Date", "Milestone", "Description", "Review status"], milestones),
        ("Challenges", ["Reported", "Category", "Description", "Status", "Review"], challenges),
        ("Progress history", ["Period", "Submitted at", "Submitted by", "Status"], progress),
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
                    row[2],
                    f"{row[3]} / {row[4]}",
                ]
                for row in challenges
            ]
        )
        pdf_rows.extend(
            [["Progress", row[0], "Submission", row[1], row[3]] for row in progress]
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
        period = data["reporting_period"]
    else:
        businesses = businesses.none()
        period = None
    accounts = IncubateeAccount.objects.filter(
        business_links__incubatee__in=businesses
    ).distinct()
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
    upcoming_sessions = MentoringSession.objects.filter(
        incubatee__in=businesses,
        session_date__gte=timezone.localdate(),
    ).select_related("incubatee", "mentor").order_by("session_date")
    if period:
        period_end = period.replace(day=monthrange(period.year, period.month)[1])
        pending_documents = pending_documents.filter(
            created_at__date__range=(period, period_end)
        )
        open_requests = open_requests.filter(
            submitted_at__date__range=(period, period_end)
        )
        upcoming_sessions = upcoming_sessions.filter(
            session_date__range=(period, period_end)
        )
    category_counts = accounts.values("entrepreneur_category").annotate(
        total=Count("pk", distinct=True)
    )
    category_map = {row["entrepreneur_category"]: row["total"] for row in category_counts}
    context = {
        "monitoring_filter": form,
        "monitoring_incubatee_count": accounts.count(),
        "monitoring_student_count": category_map.get(
            IncubateeAccount.EntrepreneurCategory.STUDENT_ENTREPRENEUR, 0
        ),
        "monitoring_external_count": category_map.get(
            IncubateeAccount.EntrepreneurCategory.EXTERNAL_SME, 0
        ),
        "monitoring_active_count": businesses.filter(
            status=Incubatee.Status.ACTIVE
        ).count(),
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
