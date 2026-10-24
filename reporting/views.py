from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from django.views.decorators.http import require_GET
from django.contrib.auth.decorators import login_required

from accounts.access import accessible_queryset, has_access
from accounts.models import Role, RoleGrant
from audit.models import AuditEvent
from audit.services import log_event
from documents.models import Document, ExpiryAlert
from documents.services import accessible_documents
from incubation.access import is_aggregate_only
from incubation.metrics import data_completeness
from incubation.models import Incubatee, MonthlyCheckIn
from interactions.models import Interaction
from partners.models import MOU, MOUExpiryAlert, Partner
from reporting.metrics import (
    dashboard_data,
    data_quality_rows,
    impact_report,
    is_board_aggregate,
)
from stakeholders.access import can_view_contact_details
from stakeholders.models import Stakeholder


def _role_context(request):
    user = request.user
    return {
        "dashboard_roles": (
            Role.objects.all()
            if user.is_superuser
            else Role.objects.filter(users=user)
        ).order_by("code"),
        "can_manage_users": user.is_superuser
        or user.roles.filter(code__in=("super_admin", "system_admin")).exists(),
        "can_manage_role_matrix": user.is_superuser
        or user.roles.filter(code="super_admin").exists(),
        "can_view_audit_log": has_access(
            user, RoleGrant.Resource.AUDIT_LOG, RoleGrant.Action.READ
        ),
        "can_view_reports": has_access(
            user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
        ),
    }


class DashboardIndexView(LoginRequiredMixin, View):
    def get(self, request):
        context = _role_context(request)
        context["roles"] = context["dashboard_roles"]
        context["can_view_reports"] = has_access(
            request.user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
        )
        context["can_view_audit_log"] = has_access(
            request.user, RoleGrant.Resource.AUDIT_LOG, RoleGrant.Action.READ
        )
        return render(request, "reporting/dashboard_index.html", context)


class RoleDashboardView(LoginRequiredMixin, UserPassesTestMixin, View):
    role_code = None

    def test_func(self):
        user = self.request.user
        return user.is_superuser or user.roles.filter(code=self.role_code).exists()

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied

    def get(self, request):
        if not has_access(
            request.user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
        ):
            if request.user.roles.filter(code=Role.Code.SYSTEM_ADMIN).exists():
                return render(
                    request,
                    "reporting/role_dashboard.html",
                    {
                        **_role_context(request),
                        "role": Role.objects.get(code=self.role_code),
                        "system_health_only": True,
                    },
                )
            raise PermissionDenied
        data = dashboard_data(request.user)
        context = {
            **_role_context(request),
            "role": Role.objects.get(code=self.role_code),
            **data,
            "quality_rows": data_quality_rows(request.user),
            "can_view_impact": has_access(
                request.user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
            ),
            "can_export_dashboard": has_access(
                request.user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
            ),
            "is_board_aggregate": is_board_aggregate(request.user),
            "today": timezone.localdate(),
            "chart_status_labels": [
                item["label"] for item in data["incubatee_status_counts"]
            ],
            "chart_status_values": [
                item["count"] for item in data["incubatee_status_counts"]
            ],
        }
        return render(request, "reporting/role_dashboard.html", context)


class ImpactDashboardView(LoginRequiredMixin, View):
    def get(self, request):
        if not has_access(
            request.user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
        ):
            raise PermissionDenied
        date_from = _parse_report_date(request.GET.get("date_from"))
        date_to = _parse_report_date(request.GET.get("date_to"))
        if request.GET.get("date_from") and date_from is None:
            messages.error(request, "Enter a valid start date.")
        if request.GET.get("date_to") and date_to is None:
            messages.error(request, "Enter a valid end date.")
        if date_from and date_to and date_to < date_from:
            raise PermissionDenied("Report end date must not precede its start date.")
        report = impact_report(
            request.user,
            date_from=date_from,
            date_to=date_to,
        )
        return render(
            request,
            "reporting/impact_dashboard.html",
            {
                **_role_context(request),
                **report,
                "is_board_aggregate": is_board_aggregate(request.user),
                "chart_categories": [item["label"] for item in report["monthly_trend"]],
                "chart_jobs": [
                    item["jobs_created"] for item in report["monthly_trend"]
                ],
                "chart_funding": [
                    (
                        str(item["funding_raised_zar"])
                        if item["funding_raised_zar"] is not None
                        else None
                    )
                    for item in report["monthly_trend"]
                ],
            },
        )


def _parse_report_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _period(request):
    date_from = _parse_report_date(request.GET.get("date_from"))
    date_to = _parse_report_date(request.GET.get("date_to"))
    if request.GET.get("date_from") and date_from is None:
        raise PermissionDenied("Invalid report start date.")
    if request.GET.get("date_to") and date_to is None:
        raise PermissionDenied("Invalid report end date.")
    if date_from and date_to and date_to < date_from:
        raise PermissionDenied("Report end date must not precede its start date.")
    return date_from, date_to


def _decimal_text(value):
    return "" if value is None else str(value)


def _workbook_response(filename, sheets):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    first = True
    for sheet_name, headers, rows in sheets:
        worksheet = workbook.active if first else workbook.create_sheet()
        first = False
        worksheet.title = sheet_name[:31]
        worksheet.append(headers)
        for cell in worksheet[1]:
            cell.font = Font(bold=True)
            cell.data_type = "s"
        for row in rows:
            worksheet.append(row)
            for cell in worksheet[worksheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column in worksheet.columns:
            width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 40)
            worksheet.column_dimensions[column[0].column_letter].width = width
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    workbook.save(response)
    return response


def _pdf_response(filename, title, rows):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    document = SimpleDocTemplate(
        response,
        pagesize=landscape(letter),
        rightMargin=0.4 * inch,
        leftMargin=0.4 * inch,
        topMargin=0.4 * inch,
        bottomMargin=0.4 * inch,
    )
    styles = getSampleStyleSheet()
    table_data = [
        [Paragraph(escape(str(value)), styles["BodyText"]) for value in row]
        for row in rows
    ]
    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0d6efd")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    document.build(
        [
            Paragraph(title, styles["Title"]),
            Spacer(1, 0.2 * inch),
            table,
        ]
    )
    return response


def _impact_export_sheets(report):
    summary = [
        ["Reporting period start", report["date_from"] or "All available"],
        ["Reporting period end", report["date_to"] or "All available"],
        [
            "Businesses represented",
            report["business_count"]
            if report["business_count"] is not None
            else "Suppressed",
        ],
        [
            "Jobs created",
            report["jobs_created"]
            if report["jobs_created"] is not None
            else report["jobs_status"].replace("_", " ").title(),
        ],
        [
            "Funding raised (ZAR)",
            _decimal_text(report["funding_raised_zar"])
            or report["funding_status"].replace("_", " ").title(),
        ],
        [
            "Mean revenue growth (%)",
            _decimal_text(report["revenue_growth_percent"])
            or report["revenue_growth_status"].replace("_", " ").title(),
        ],
        [
            "Businesses with revenue comparison",
            report["revenue_growth_businesses"]
            if report["revenue_growth_businesses"] is not None
            else "Suppressed",
        ],
    ]
    sheets = [
        ("Summary", ["Measure", "Value"], summary),
        (
            "Monthly trend",
            ["Period", "Jobs created", "Funding received (ZAR)"],
            [
                [
                    row["label"],
                    row["jobs_created"]
                    if row["jobs_created"] is not None
                    else row["jobs_status"].replace("_", " ").title(),
                    _decimal_text(row["funding_raised_zar"])
                    or row["funding_status"].replace("_", " ").title(),
                ]
                for row in report["monthly_trend"]
            ],
        ),
    ]
    for dimension, buckets in report["breakdowns"].items():
        if dimension in {"gender", "youth"}:
            sheets.append(
                (
                    dimension.title(),
                    ["Category", "Founder count"],
                    [
                        [bucket["label"], bucket["count"]]
                        for bucket in buckets
                    ],
                )
            )
            continue
        sheets.append(
            (
                dimension.title(),
                [
                    "Category",
                    "Businesses",
                    "Jobs created",
                    "Funding raised (ZAR)",
                    "Mean revenue growth (%)",
                ],
                [
                    [
                        bucket["label"],
                        bucket.get("businesses", ""),
                        bucket.get("jobs_created", ""),
                        _decimal_text(bucket.get("funding_raised_zar"))
                        or bucket.get("funding_status", "missing").replace("_", " ").title(),
                        _decimal_text(bucket.get("revenue_growth_percent"))
                        or bucket.get("revenue_growth_status", "missing").replace("_", " ").title(),
                    ]
                    for bucket in buckets
                ],
            )
        )
    return sheets


@login_required
def impact_export(request, format_name):
    if format_name not in {"xlsx", "pdf"}:
        raise PermissionDenied
    if not has_access(
        request.user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
    ):
        raise PermissionDenied
    date_from, date_to = _period(request)
    report = impact_report(request.user, date_from=date_from, date_to=date_to)
    sheets = _impact_export_sheets(report)
    if format_name == "xlsx":
        response = _workbook_response("impact-report.xlsx", sheets)
    else:
        rows = [
            ["Category", "Businesses", "Jobs created", "Funding (ZAR)", "Revenue growth (%)"],
            [
                f"Period: {date_from or 'All available'} to {date_to or 'Latest available'}",
                report["business_count"] if report["business_count"] is not None else "Suppressed",
                report["jobs_created"]
                if report["jobs_created"] is not None
                else report["jobs_status"].replace("_", " ").title(),
                _decimal_text(report["funding_raised_zar"])
                or report["funding_status"].replace("_", " ").title(),
                _decimal_text(report["revenue_growth_percent"])
                or report["revenue_growth_status"].replace("_", " ").title(),
            ],
        ]
        for dimension, buckets in report["breakdowns"].items():
            for bucket in buckets:
                if dimension in {"gender", "youth"}:
                    rows.append(
                        [
                            f"{dimension.title()}: {bucket['label']}",
                            bucket["count"] if bucket["count"] is not None else "Suppressed",
                            "",
                            "",
                            "",
                        ]
                    )
                    continue
                rows.append(
                    [
                        f"{dimension.title()}: {bucket['label']}",
                        bucket.get("businesses", ""),
                        bucket.get("jobs_created")
                        if bucket.get("jobs_created") is not None
                        else bucket.get("jobs_status", "missing").replace("_", " ").title(),
                        _decimal_text(bucket.get("funding_raised_zar"))
                        or bucket.get("funding_status", "missing").replace("_", " ").title(),
                        _decimal_text(bucket.get("revenue_growth_percent"))
                        or bucket.get("revenue_growth_status", "missing").replace("_", " ").title(),
                    ]
                )
        rows.extend(
            [
                ["Monthly trend", "", "", "", ""],
                ["Period", "Businesses", "Jobs created", "Funding (ZAR)", "Revenue growth (%)"],
            ]
        )
        rows.extend(
            [
                [
                    row["label"],
                    "",
                    row["jobs_created"]
                    if row["jobs_created"] is not None
                    else row["jobs_status"].replace("_", " ").title(),
                    _decimal_text(row["funding_raised_zar"])
                    or row["funding_status"].replace("_", " ").title(),
                    "",
                ]
                for row in report["monthly_trend"]
            ]
        )
        response = _pdf_response("impact-report.pdf", "UMP CFERI Impact Report", rows)
    log_event(
        actor=request.user,
        action=AuditEvent.Action.EXPORT,
        resource="reporting.impact",
        metadata={"format": format_name},
        request=request,
    )
    return response


def _interaction_export_rows(user, request):
    interactions = accessible_queryset(
        user,
        RoleGrant.Resource.INTERACTIONS,
        RoleGrant.Action.READ,
        Interaction.objects.select_related("created_by").prefetch_related(
            "participant_links__stakeholder", "incubatees", "partners"
        ),
    )
    interaction_type = request.GET.get("interaction_type")
    if interaction_type in Interaction.Type.values:
        interactions = interactions.filter(interaction_type=interaction_type)
    date_from, date_to = _period(request)
    if date_from:
        interactions = interactions.filter(occurred_at__date__gte=date_from)
    if date_to:
        interactions = interactions.filter(occurred_at__date__lte=date_to)
    incubatee_id = request.GET.get("incubatee")
    if incubatee_id:
        interactions = interactions.filter(incubatees__pk=incubatee_id)
    partner_id = request.GET.get("partner")
    if partner_id:
        interactions = interactions.filter(partners__pk=partner_id)
    rows = []
    for interaction in interactions.distinct():
        if not has_access(
            user,
            RoleGrant.Resource.INTERACTIONS,
            RoleGrant.Action.READ,
            interaction,
        ):
            continue
        participants = [
            link.stakeholder.name if link.stakeholder_id else link.display_name
            for link in interaction.participant_links.select_related("stakeholder")
            if not is_board_aggregate(user)
            and (not link.stakeholder_id
            or has_access(
                user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.READ,
                link.stakeholder,
            ))
        ]
        incubatees = [
            str(record)
            for record in interaction.incubatees.all()
            if has_access(
                user, RoleGrant.Resource.INCUBATEES, RoleGrant.Action.READ, record
            )
        ]
        partners = [
            record.name
            for record in interaction.partners.all()
            if has_access(
                user, RoleGrant.Resource.PARTNERS_MOUS, RoleGrant.Action.READ, record
            )
        ]
        rows.append(
            [
                interaction.occurred_at.isoformat(),
                interaction.get_interaction_type_display(),
                interaction.duration_minutes,
                "; ".join(participants),
                "; ".join(incubatees),
                "; ".join(partners),
                "" if is_board_aggregate(user) else interaction.topics,
                "" if is_board_aggregate(user) else interaction.outcome,
            ]
        )
    return rows


@login_required
def interaction_export(request, format_name):
    if format_name not in {"xlsx", "pdf"}:
        raise PermissionDenied
    if not has_access(
        request.user, RoleGrant.Resource.INTERACTIONS, RoleGrant.Action.READ
    ):
        raise PermissionDenied
    rows = _interaction_export_rows(request.user, request)
    headers = [
        "Date/time",
        "Type",
        "Duration (minutes)",
        "Participants",
        "Incubatees",
        "Partners",
        "Topics",
        "Outcome",
    ]
    response = (
        _workbook_response("interactions.xlsx", [("Interactions", headers, rows)])
        if format_name == "xlsx"
        else _pdf_response("interactions.pdf", "UMP CFERI Interactions", [headers, *rows])
    )
    log_event(
        actor=request.user,
        action=AuditEvent.Action.EXPORT,
        resource="reporting.interactions",
        metadata={"format": format_name},
        request=request,
    )
    return response


@login_required
def dashboard_export(request, role_code, format_name):
    if format_name not in {"xlsx", "pdf"}:
        raise PermissionDenied
    if role_code not in Role.Code.values or not (
        request.user.is_superuser
        or request.user.roles.filter(code=role_code).exists()
    ):
        raise PermissionDenied
    if not has_access(
        request.user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
    ):
        raise PermissionDenied
    data = dashboard_data(request.user)
    quality_rows = data_quality_rows(request.user)
    overdue_rows = list(data["overdue_checkins"])
    summary = [
        [
            "Incubatees in permitted scope",
            data["incubatee_total"]
            if data["incubatee_total"] is not None
            else "Suppressed",
        ],
        ["Active MOUs", data["active_mou_count"]],
        ["Expiring / expired documents in 60 days", len(data["expiring_documents"])],
        [
            "Overdue monthly check-ins",
            data["overdue_checkin_count"]
            if data["overdue_checkin_count"] is not None
            else "Suppressed",
        ],
    ]
    sheets = [
        ("Summary", ["Measure", "Value"], summary),
        (
            "Incubatees by status",
            ["Status", "Count"],
            [
                [
                    row["label"],
                    row["count"] if row["count"] is not None else "Suppressed",
                ]
                for row in data["incubatee_status_counts"]
            ],
        ),
        (
            "Expiring documents",
            ["Title", "Expiry date", "Status"],
            [
                [
                    document.title,
                    document.expiry_date.isoformat(),
                    "Expired" if document.expiry_date < timezone.localdate() else "Expiring",
                ]
                for document in data["expiring_documents"]
            ],
        ),
        (
            "Overdue check-ins",
            ["Incubatee", "Period", "Due date", "Status"],
            [
                [
                    checkin.incubatee.legal_name,
                    checkin.period_start.strftime("%B %Y"),
                    checkin.due_date.isoformat(),
                    "Overdue",
                ]
                for checkin in overdue_rows
            ],
        ),
        (
            "Data quality",
            [
                "Incubatee",
                "Profile completeness",
                "Missing tracked fields",
                "Duplicate warning",
                "Overdue",
                "Latest check-in completeness",
            ],
            [
                [
                    row["incubatee"].legal_name,
                    f"{row['profile_completeness']}%",
                    ", ".join(row["missing"]),
                    "Review possible duplicate" if row["possible_duplicate"] else "",
                    "Yes" if row["overdue"] else "No",
                    f"{row['completeness']}%"
                    if row["completeness"] is not None
                    else "No submitted check-in",
                ]
                for row in quality_rows
            ],
        ),
    ]
    for document in data["expiring_documents"]:
        log_event(
            actor=request.user,
            action=AuditEvent.Action.DOCUMENT_VIEW,
            target=document,
            resource="documents.centre_document",
            request=request,
        )
    if format_name == "xlsx":
        response = _workbook_response(
            f"{role_code}-dashboard.xlsx", sheets
        )
    else:
        rows = [
            ["Measure", "Value"],
            *summary,
            ["Incubatees by status", ""],
            ["Status", "Count"],
            *sheets[1][2],
            ["Expiring documents", ""],
            ["Title", "Expiry date", "Status"],
            *sheets[2][2],
            ["Overdue check-ins", ""],
            ["Incubatee", "Period", "Due date", "Status"],
            *sheets[3][2],
            ["Data quality", ""],
            *[sheets[4][1]],
            *sheets[4][2],
        ]
        response = _pdf_response(
            f"{role_code}-dashboard.pdf",
            f"UMP CFERI {role_code.replace('_', ' ').title()} Dashboard",
            rows,
        )
    log_event(
        actor=request.user,
        action=AuditEvent.Action.EXPORT,
        resource="reporting.dashboard",
        metadata={"format": format_name, "role": role_code},
        request=request,
    )
    return response


def _list_export_data(request, dataset):
    user = request.user
    if dataset == "incubatees":
        if is_aggregate_only(user):
            raise PermissionDenied
        resource = RoleGrant.Resource.INCUBATEES
        if not has_access(user, resource, RoleGrant.Action.READ):
            raise PermissionDenied
        records = accessible_queryset(
            user,
            resource,
            RoleGrant.Action.READ,
            Incubatee.objects.select_related(
                "programme", "cohort", "staff_owner"
            ),
        )
        query = request.GET.get("q", "").strip()
        status = request.GET.get("status", "")
        if query:
            records = records.filter(
                Q(legal_name__icontains=query) | Q(trading_name__icontains=query)
            )
        if status in Incubatee.Status.values:
            records = records.filter(status=status)
        return (
            "Incubatees",
            ["Business", "Status", "Programme", "Cohort", "Staff owner", "Updated"],
            [
                [
                    record.trading_name or record.legal_name,
                    record.get_status_display(),
                    record.programme.name if record.programme_id else "",
                    record.cohort.name if record.cohort_id else "",
                    record.staff_owner.email if record.staff_owner_id else "",
                    record.updated_at.date().isoformat(),
                ]
                for record in records.order_by("legal_name", "pk")
                if has_access(user, resource, RoleGrant.Action.READ, record)
            ],
        )

    if dataset == "partners":
        resource = RoleGrant.Resource.PARTNERS_MOUS
        if not has_access(user, resource, RoleGrant.Action.READ):
            raise PermissionDenied
        records = accessible_queryset(
            user,
            resource,
            RoleGrant.Action.READ,
            Partner.objects.all(),
        )
        query = request.GET.get("q", "").strip()
        organisation_type = request.GET.get("type", "")
        if query:
            records = records.filter(
                Q(name__icontains=query) | Q(registration_number__icontains=query)
            )
        if organisation_type in Partner.OrganisationType.values:
            records = records.filter(organisation_type=organisation_type)
        return (
            "Partners",
            ["Organisation", "Type", "Sector", "Province", "Registration number"],
            [
                [
                    record.name,
                    record.get_organisation_type_display(),
                    record.sector,
                    record.province,
                    record.registration_number,
                ]
                for record in records.order_by("name", "pk")
                if has_access(user, resource, RoleGrant.Action.READ, record)
            ],
        )

    if dataset == "mous":
        resource = RoleGrant.Resource.PARTNERS_MOUS
        if not has_access(user, resource, RoleGrant.Action.READ):
            raise PermissionDenied
        readable_partners = accessible_queryset(
            user, resource, RoleGrant.Action.READ, Partner.objects.all()
        )
        records = accessible_queryset(
            user,
            resource,
            RoleGrant.Action.READ,
            MOU.objects.filter(partner_id__in=readable_partners.values("pk"))
            .select_related("partner"),
        )
        status = request.GET.get("status", "")
        partner_id = request.GET.get("partner", "")
        if status in MOU.Status.values:
            records = records.filter(status=status)
        if partner_id:
            records = records.filter(partner_id=partner_id)
        return (
            "MOUs",
            ["Agreement", "Partner", "Reference", "Status", "End date", "Value (ZAR)"],
            [
                [
                    record.title,
                    record.partner.name,
                    record.reference,
                    record.get_status_display(),
                    record.end_date.isoformat() if record.end_date else "",
                    str(record.value_zar) if record.value_zar is not None else "",
                ]
                for record in records.order_by("-created_at", "title")
                if has_access(user, resource, RoleGrant.Action.READ, record)
                and has_access(user, resource, RoleGrant.Action.READ, record.partner)
            ],
        )

    if dataset == "stakeholders":
        resource = RoleGrant.Resource.STAKEHOLDERS
        if not has_access(user, resource, RoleGrant.Action.READ):
            raise PermissionDenied
        records = accessible_queryset(
            user,
            resource,
            RoleGrant.Action.READ,
            Stakeholder.objects.prefetch_related("type_assignments"),
        )
        query = request.GET.get("q", "").strip()
        can_search_contact = user.is_superuser or user.roles.filter(
            code__in=(Role.Code.SUPER_ADMIN, Role.Code.ADMIN, Role.Code.CFERI_STAFF)
        ).exists()
        if query:
            search = Q(name__icontains=query) | Q(title__icontains=query)
            if can_search_contact:
                search |= Q(email__icontains=query) | Q(phone__icontains=query)
            records = records.filter(search)
        rows = []
        for record in records.order_by("name", "email"):
            if not has_access(user, resource, RoleGrant.Action.READ, record):
                continue
            relationships = ", ".join(
                assignment.get_relationship_type_display()
                for assignment in record.type_assignments.all()
                if assignment.is_active
            )
            may_view_contact = can_view_contact_details(user, record)
            rows.append(
                [
                    record.name,
                    record.title,
                    relationships,
                    record.email if may_view_contact else "Restricted",
                    record.phone if may_view_contact else "Restricted",
                ]
            )
        return (
            "Stakeholders",
            ["Name", "Position / title", "Active relationships", "Email", "Phone"],
            rows,
        )

    if dataset == "documents":
        if not (
            has_access(
                user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
            or has_access(
                user,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
        ):
            raise PermissionDenied
        records = accessible_documents(
            user, include_archived=request.GET.get("archived") == "1"
        )
        query = request.GET.get("q", "").strip().casefold()
        category = request.GET.get("category", "")
        status = request.GET.get("status", "")
        if query:
            records = [
                record
                for record in records
                if any(
                    query in value.casefold()
                    for value in (
                        record.title,
                        record.reference_number,
                        record.issuing_authority,
                        record.notes,
                        " ".join(record.tags),
                        record.get_category_display(),
                        record.issue_date.isoformat() if record.issue_date else "",
                        record.expiry_date.isoformat() if record.expiry_date else "",
                        record.get_sensitivity_display(),
                        record.owner.get_full_name(),
                        record.owner.email,
                    )
                )
            ]
        if category:
            records = [record for record in records if record.category == category]
        if status:
            records = [record for record in records if record.expiry_status == status]
        for record in records:
            AuditEvent.objects.create(
                actor=user,
                action=AuditEvent.Action.DOCUMENT_VIEW,
                resource="documents.centre_document",
                object_id=str(record.pk),
                document_uuid=record.pk,
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
            )
        return (
            "Documents",
            ["Title", "Category", "Sensitivity", "Owner", "Expiry", "Status"],
            [
                [
                    record.title,
                    record.get_category_display(),
                    record.get_sensitivity_display(),
                    record.owner.get_full_name() or record.owner.email,
                    record.expiry_date.isoformat() if record.expiry_date else "",
                    record.expiry_status.replace("_", " ").title(),
                ]
                for record in records
            ],
        )

    if dataset == "compliance":
        if not (
            has_access(
                user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
            or has_access(
                user,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
        ):
            raise PermissionDenied
        records = accessible_documents(user)
        for record in records:
            log_event(
                actor=user,
                action=AuditEvent.Action.DOCUMENT_VIEW,
                target=record,
                resource="documents.centre_document",
                request=request,
            )
        return (
            "Compliance status",
            ["Document", "Category", "Sensitivity", "Expiry date", "Status"],
            [
                [
                    record.title,
                    record.get_category_display(),
                    record.get_sensitivity_display(),
                    record.expiry_date.isoformat() if record.expiry_date else "",
                    record.expiry_status.replace("_", " ").title(),
                ]
                for record in records
            ],
        )

    if dataset == "checkins":
        resource = RoleGrant.Resource.INCUBATEES
        if not has_access(user, resource, RoleGrant.Action.READ):
            raise PermissionDenied
        accessible_incubatees = accessible_queryset(
            user, resource, RoleGrant.Action.READ, Incubatee.objects.all()
        )
        records = MonthlyCheckIn.objects.filter(
            incubatee__in=accessible_incubatees
        ).select_related("incubatee").prefetch_related("observations")
        rows = []
        for record in records.order_by("-period_start", "incubatee__legal_name"):
            if not has_access(user, resource, RoleGrant.Action.READ, record.incubatee):
                continue
            completeness = data_completeness(record)
            state = (
                "Complete"
                if record.submitted_at
                else "Overdue"
                if record.due_date and record.due_date < timezone.localdate()
                else "Pending"
            )
            rows.append(
                [
                    record.incubatee.legal_name,
                    record.period_start.strftime("%B %Y"),
                    record.submitted_at.isoformat() if record.submitted_at else "Not submitted",
                    f"{completeness['score']}%",
                    f"{completeness['missing']} missing, {completeness['not_applicable']} N/A",
                    record.due_date.isoformat() if record.due_date else "Not configured",
                    state,
                ]
            )
        return (
            "Monthly check-ins",
            ["Incubatee", "Period", "Submitted", "Completeness", "Missing / N/A", "Due", "State"],
            rows,
        )

    if dataset == "document_alerts":
        if not (
            has_access(
                user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
            or has_access(
                user,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
        ):
            raise PermissionDenied
        documents = accessible_documents(user)
        alerts = ExpiryAlert.objects.filter(
            document_id__in=[document.pk for document in documents],
            document__archived=False,
            expiry_date=F("document__expiry_date"),
        ).select_related("document").order_by("due_date")
        for alert in alerts:
            log_event(
                actor=user,
                action=AuditEvent.Action.DOCUMENT_VIEW,
                target=alert.document,
                resource="documents.centre_document",
                request=request,
            )
        return (
            "Document expiry alerts",
            ["Document", "Expiry date", "Reminder", "Due date", "Email status"],
            [
                [
                    alert.document.title,
                    alert.expiry_date.isoformat(),
                    f"{alert.days_before}-day reminder",
                    alert.due_date.isoformat(),
                    "Sent"
                    if alert.email_sent_at
                    else "Failed"
                    if alert.email_error
                    else "Pending",
                ]
                for alert in alerts
                if has_access(
                    user,
                    alert.document.permission_resource,
                    RoleGrant.Action.READ,
                    alert.document,
                )
            ],
        )

    if dataset == "mou_alerts":
        resource = RoleGrant.Resource.PARTNERS_MOUS
        if not has_access(user, resource, RoleGrant.Action.READ):
            raise PermissionDenied
        readable_partners = accessible_queryset(
            user, resource, RoleGrant.Action.READ, Partner.objects.all()
        )
        accessible_mous = accessible_queryset(
            user,
            resource,
            RoleGrant.Action.READ,
            MOU.objects.filter(partner_id__in=readable_partners.values("pk")),
        )
        alerts = MOUExpiryAlert.objects.filter(
            mou_id__in=accessible_mous.values("pk"),
            mou__status=MOU.Status.ACTIVE,
            end_date=F("mou__end_date"),
        ).select_related("mou", "mou__partner")
        return (
            "MOU renewal alerts",
            ["MOU", "Partner", "End date", "Reminder", "Due date", "Email status"],
            [
                [
                    alert.mou.title,
                    alert.mou.partner.name,
                    alert.end_date.isoformat(),
                    f"{alert.days_before}-day reminder",
                    alert.due_date.isoformat(),
                    "Sent"
                    if alert.email_sent_at
                    else "Failed"
                    if alert.email_error
                    else "Pending",
                ]
                for alert in alerts
                if has_access(user, resource, RoleGrant.Action.READ, alert.mou)
                and has_access(
                    user, resource, RoleGrant.Action.READ, alert.mou.partner
                )
            ],
        )

    if dataset == "audit":
        if not has_access(user, RoleGrant.Resource.AUDIT_LOG, RoleGrant.Action.READ):
            raise PermissionDenied
        events = AuditEvent.objects.select_related("actor")
        action = request.GET.get("action", "")
        if action in AuditEvent.Action.values:
            events = events.filter(action=action)
        resource_filter = request.GET.get("resource", "").strip()[:64]
        if resource_filter:
            events = events.filter(resource=resource_filter)
        return (
            "Audit log",
            ["Timestamp", "Actor", "Action", "Resource", "Record", "Outcome", "Metadata", "IP address"],
            [
                [
                    event.occurred_at.isoformat(),
                    str(event.actor) if event.actor_id else "System",
                    event.get_action_display(),
                    event.resource,
                    event.object_id,
                    event.outcome,
                    str(event.metadata),
                    event.ip_address or "",
                ]
                for event in events[:500]
            ],
        )

    raise PermissionDenied


@login_required
def list_export(request, dataset, format_name):
    if format_name not in {"xlsx", "pdf"}:
        raise PermissionDenied
    sheet_name, headers, rows = _list_export_data(request, dataset)
    filename = f"{dataset}.{format_name}"
    response = (
        _workbook_response(filename, [(sheet_name, headers, rows)])
        if format_name == "xlsx"
        else _pdf_response(filename, f"UMP CFERI {sheet_name}", [headers, *rows])
    )
    log_event(
        actor=request.user,
        action=AuditEvent.Action.EXPORT,
        resource=f"reporting.{dataset}",
        metadata={"format": format_name, "row_count": len(rows)},
        request=request,
    )
    return response


@login_required
@require_GET
def analytics_api(request):
    if not has_access(
        request.user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
    ):
        raise PermissionDenied
    date_from, date_to = _period(request)
    report = impact_report(request.user, date_from=date_from, date_to=date_to)
    safe_data = {
        "period": {
            "start": date_from.isoformat() if date_from else None,
            "end": date_to.isoformat() if date_to else None,
        },
        "business_count": report["business_count"],
        "jobs_created": report["jobs_created"],
        "jobs_status": report["jobs_status"],
        "funding_raised_zar": (
            str(report["funding_raised_zar"])
            if report["funding_raised_zar"] is not None
            else None
        ),
        "funding_status": report["funding_status"],
        "revenue_growth_percent": (
            str(report["revenue_growth_percent"])
            if report["revenue_growth_percent"] is not None
            else None
        ),
        "revenue_growth_businesses": report["revenue_growth_businesses"],
        "revenue_growth_status": report["revenue_growth_status"],
        "monthly_trend": [
            {
                "period": row["period_start"].isoformat(),
                "jobs_created": row["jobs_created"],
                "jobs_status": row["jobs_status"],
                "funding_raised_zar": (
                    str(row["funding_raised_zar"])
                    if row["funding_raised_zar"] is not None
                    else None
                ),
                "funding_status": row["funding_status"],
            }
            for row in report["monthly_trend"]
        ],
        "breakdowns": {
            dimension: [
                {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in bucket.items()
                    if key != "business_ids"
                }
                for bucket in buckets
            ]
            for dimension, buckets in report["breakdowns"].items()
        },
    }
    log_event(
        actor=request.user,
        action=AuditEvent.Action.EXPORT,
        resource="reporting.analytics_api",
        metadata={"format": "json"},
        request=request,
    )
    response = JsonResponse(safe_data)
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def audit_log(request):
    if not has_access(
        request.user, RoleGrant.Resource.AUDIT_LOG, RoleGrant.Action.READ
    ):
        raise PermissionDenied
    events = AuditEvent.objects.select_related("actor", "content_type")
    action = request.GET.get("action", "")
    if action in AuditEvent.Action.values:
        events = events.filter(action=action)
    resource = request.GET.get("resource", "").strip()[:64]
    if resource:
        events = events.filter(resource=resource)
    return render(
        request,
        "audit/audit_log.html",
        {
            **_role_context(request),
            "events": events[:500],
            "actions": AuditEvent.Action.choices,
            "selected_action": action,
            "resource": resource,
            "can_export": True,
        },
    )
