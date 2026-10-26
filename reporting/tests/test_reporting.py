from io import BytesIO
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from accounts.models import RecordShare, Role, TOTPDevice
from audit.models import AuditEvent
from documents.models import Document, ExpiryAlert
from incubation.models import Incubatee, IncubateeMetricObservation, MonthlyCheckIn
from interactions.models import Interaction, InteractionParticipant
from partners.models import MOU, MOUExpiryAlert, Partner
from reporting.tasks import send_monthly_summary_reports
from stakeholders.models import Stakeholder

User = get_user_model()


class ReportingAndAuditTests(TestCase):
    def create_user(self, email, role_code):
        user = User.objects.create_user(email=email, password="Synthetic-test-password-123!")
        user.roles.add(Role.objects.get(code=role_code))
        if role_code in {
            Role.Code.SUPER_ADMIN,
            Role.Code.SYSTEM_ADMIN,
            Role.Code.ADMIN,
            Role.Code.BOARD_MEMBER,
        }:
            TOTPDevice.objects.create(
                user=user,
                secret_encrypted="synthetic-test-secret",
                confirmed=True,
            )
        return user

    def setUp(self):
        self.admin = self.create_user("reporting-admin@gmail.com", Role.Code.ADMIN)
        self.board = self.create_user(
            "reporting-board@gmail.com", Role.Code.BOARD_MEMBER
        )
        self.system_admin = self.create_user(
            "reporting-system@gmail.com", Role.Code.SYSTEM_ADMIN
        )

    def test_impact_report_uses_saved_records_and_suppresses_small_board_cells(self):
        incubatee = Incubatee.objects.create(
            legal_name="Synthetic Reporting Company",
            monthly_average_revenue_zar=Decimal("1000.00"),
            created_by=self.admin,
        )
        IncubateeMetricObservation.objects.create(
            incubatee=incubatee,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            period_start=timezone.localdate().replace(day=1),
            metric=IncubateeMetricObservation.Metric.JOBS_CREATED,
            integer_value=2,
            source="synthetic_test",
        )
        from reporting.metrics import impact_report

        report = impact_report(self.board)

        self.assertIsNone(report["business_count"])
        self.assertIsNone(report["jobs_created"])
        self.assertEqual(report["jobs_contributors"], 1)
        self.assertEqual(report["incubatees"], [incubatee])
        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditEvent.Action.CREATE,
                resource="incubatee",
                object_id=str(incubatee.pk),
            ).exists()
        )

    def login_with_mfa(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["mfa_verified_user"] = user.pk
        session.save()

    def test_impact_dashboard_and_authorized_xlsx_pdf_exports(self):
        self.login_with_mfa(self.admin)

        dashboard = self.client.get(reverse("reporting:impact"))
        xlsx = self.client.get(reverse("reporting:impact_export", args=("xlsx",)))
        pdf = self.client.get(reverse("reporting:impact_export", args=("pdf",)))

        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Impact dashboard")
        self.assertEqual(xlsx.status_code, 200)
        self.assertEqual(
            xlsx["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        workbook = load_workbook(BytesIO(xlsx.content), read_only=True)
        self.assertIn("Summary", workbook.sheetnames)
        self.assertIn("Monthly trend", workbook.sheetnames)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_role_dashboards_render_with_authorized_scopes(self):
        for user, route in (
            (self.admin, "reporting:admin"),
            (self.board, "reporting:board_member"),
        ):
            self.login_with_mfa(user)
            response = self.client.get(reverse(route))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Incubatees by status")
            self.assertContains(
                response,
                f'href="{reverse("reporting:impact")}">Reports</a>',
            )
            self.assertNotContains(response, ">Impact report</a>")

    def test_dashboard_home_shows_super_admin_operational_data_not_role_links(self):
        super_admin = self.create_user(
            "reporting-super-admin@gmail.com", Role.Code.SUPER_ADMIN
        )
        incubatee = Incubatee.objects.create(
            legal_name="Synthetic Dashboard Company",
            created_by=self.admin,
        )
        self.login_with_mfa(super_admin)

        response = self.client.get(reverse("reporting:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incubatees by status")
        self.assertContains(response, "Synthetic Dashboard Company")
        self.assertNotContains(response, "Your dashboards")
        self.assertNotContains(response, "Open role dashboard")
        self.assertNotContains(response, reverse("reporting:super_admin"))
        self.assertEqual(response.context["incubatee_total"], 1)

    def test_system_admin_dashboard_shows_system_health_data(self):
        AuditEvent.objects.create(
            actor=self.admin,
            action=AuditEvent.Action.LOGIN,
            resource="accounts.user",
            object_id=str(self.admin.pk),
            outcome="failure",
        )
        self.login_with_mfa(self.system_admin)

        response = self.client.get(reverse("reporting:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active accounts")
        self.assertContains(response, "Failed sign-ins")
        self.assertContains(response, "Recent system activity")
        self.assertNotContains(response, "Incubatees by status")
        self.assertEqual(response.context["failed_login_count"], 1)

    def test_board_analytics_api_returns_only_suppressed_aggregate(self):
        incubatee = Incubatee.objects.create(
            legal_name="Synthetic Board API Company",
            created_by=self.admin,
        )
        IncubateeMetricObservation.objects.create(
            incubatee=incubatee,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            period_start=timezone.localdate().replace(day=1),
            metric=IncubateeMetricObservation.Metric.JOBS_CREATED,
            integer_value=1,
            source="synthetic_test",
        )
        self.login_with_mfa(self.board)

        response = self.client.get(reverse("reporting:analytics_api"))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["jobs_created"])
        self.assertIsNone(data["business_count"])
        self.assertNotIn("incubatees", data)
        self.assertIn("private, no-store", response["Cache-Control"])

    def test_missing_metrics_are_distinct_from_recorded_zero(self):
        incubatee = Incubatee.objects.create(
            legal_name="Synthetic Missing Versus Zero",
            created_by=self.admin,
        )
        IncubateeMetricObservation.objects.create(
            incubatee=incubatee,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            period_start=timezone.localdate().replace(day=1),
            metric=IncubateeMetricObservation.Metric.JOBS_CREATED,
            integer_value=0,
            source="synthetic_test",
        )
        from reporting.metrics import impact_report

        report = impact_report(self.admin)

        self.assertEqual(report["jobs_created"], 0)
        self.assertEqual(report["jobs_status"], "recorded")
        self.assertIsNone(report["funding_raised_zar"])
        self.assertEqual(report["funding_status"], "missing")

    def test_board_interaction_detail_redacts_personal_and_free_text_fields(self):
        interaction = Interaction.objects.create(
            occurred_at=timezone.now(),
            duration_minutes=20,
            interaction_type=Interaction.Type.MEETING,
            topics="Synthetic confidential topic marker",
            outcome="Synthetic confidential outcome marker",
            created_by=self.admin,
            owner=self.admin,
        )
        InteractionParticipant.objects.create(
            interaction=interaction,
            display_name="Synthetic participant name marker",
        )
        self.login_with_mfa(self.board)

        response = self.client.get(
            reverse("interactions:detail", args=(interaction.pk,))
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Synthetic confidential topic marker")
        self.assertNotContains(response, "Synthetic confidential outcome marker")
        self.assertNotContains(response, "Synthetic participant name marker")
        self.assertContains(response, "Participant details are restricted")

        export = self.client.get(
            reverse("reporting:interaction_export", args=("xlsx",))
        )
        workbook = load_workbook(BytesIO(export.content), read_only=True)
        exported_text = " ".join(
            str(value)
            for row in workbook["Interactions"].iter_rows(values_only=True)
            for value in row
            if value is not None
        )
        self.assertNotIn("Synthetic confidential topic marker", exported_text)
        self.assertNotIn("Synthetic confidential outcome marker", exported_text)
        self.assertNotIn("Synthetic participant name marker", exported_text)

    def test_exports_are_denied_without_report_permission(self):
        self.client.force_login(self.system_admin)
        session = self.client.session
        session["mfa_verified_user"] = self.system_admin.pk
        session.save()
        response = self.client.get(
            reverse("reporting:impact_export", args=("xlsx",))
        )
        self.assertEqual(response.status_code, 403)

    def test_authorized_list_exports_cover_primary_module_lists(self):
        incubatee = Incubatee.objects.create(
            legal_name="Synthetic Export Company",
            created_by=self.admin,
            owner=self.admin,
        )
        MonthlyCheckIn.objects.create(
            incubatee=incubatee,
            period_start=timezone.localdate().replace(day=1),
            submitted_at=timezone.now(),
            completeness_score=100,
        )
        partner = Partner.objects.create(
            name="Synthetic Export Partner",
            organisation_type=Partner.OrganisationType.NGO,
            created_by=self.admin,
            owner=self.admin,
        )
        MOU.objects.create(
            partner=partner,
            title="Synthetic Export Agreement",
            status=MOU.Status.ACTIVE,
            end_date=timezone.localdate() + timedelta(days=29),
            created_by=self.admin,
            owner=self.admin,
        )
        mou = MOU.objects.get(title="Synthetic Export Agreement")
        MOUExpiryAlert.objects.create(
            mou=mou,
            end_date=mou.end_date,
            days_before=30,
            due_date=timezone.localdate(),
        )
        Stakeholder.objects.create(
            name="Synthetic Export Contact",
            email="export-contact@gmail.com",
            created_by=self.admin,
            owner=self.admin,
        )
        Document.objects.create(
            title="Synthetic Export Document",
            category=Document.Category.POLICIES,
            expiry_date=timezone.localdate() + timedelta(days=7),
            created_by=self.admin,
            owner=self.admin,
        )
        document = Document.objects.get(title="Synthetic Export Document")
        ExpiryAlert.objects.create(
            document=document,
            expiry_date=document.expiry_date,
            days_before=7,
            due_date=timezone.localdate(),
        )
        self.login_with_mfa(self.admin)

        expected_markers = {
            "incubatees": "Synthetic Export Company",
            "checkins": "Synthetic Export Company",
            "partners": "Synthetic Export Partner",
            "mous": "Synthetic Export Agreement",
            "stakeholders": "Synthetic Export Contact",
            "documents": "Synthetic Export Document",
            "compliance": "Synthetic Export Document",
            "document_alerts": "Synthetic Export Document",
            "mou_alerts": "Synthetic Export Agreement",
        }
        for dataset, marker in expected_markers.items():
            for format_name in ("xlsx", "pdf"):
                with self.subTest(dataset=dataset, format_name=format_name):
                    response = self.client.get(
                        reverse("reporting:list_export", args=(dataset, format_name))
                    )
                    self.assertEqual(response.status_code, 200)
                    if format_name == "xlsx":
                        workbook = load_workbook(BytesIO(response.content), read_only=True)
                        exported_text = " ".join(
                            str(value)
                            for row in workbook.active.iter_rows(values_only=True)
                            for value in row
                            if value is not None
                        )
                    else:
                        self.assertTrue(response.content.startswith(b"%PDF"))
                    if format_name == "xlsx":
                        self.assertIn(marker, exported_text)

        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.admin,
                action=AuditEvent.Action.EXPORT,
                resource="reporting.documents",
            ).exists()
        )

    def test_dashboard_and_audit_exports_enforce_their_permissions(self):
        self.login_with_mfa(self.admin)
        for format_name in ("xlsx", "pdf"):
            dashboard = self.client.get(
                reverse(
                    "reporting:dashboard_export",
                    args=(Role.Code.ADMIN, format_name),
                )
            )
            self.assertEqual(dashboard.status_code, 200)
        denied = self.client.get(
            reverse(
                "reporting:dashboard_export",
                args=(Role.Code.SYSTEM_ADMIN, "xlsx"),
            )
        )
        self.assertEqual(denied.status_code, 403)

        self.login_with_mfa(self.system_admin)
        audit_export = self.client.get(
            reverse("reporting:list_export", args=("audit", "xlsx"))
        )
        self.assertEqual(audit_export.status_code, 200)
        workbook = load_workbook(BytesIO(audit_export.content), read_only=True)
        self.assertEqual(workbook.active.title, "Audit log")

    def test_board_cannot_export_individual_incubatee_records(self):
        Incubatee.objects.create(
            legal_name="Synthetic Private Company",
            created_by=self.admin,
            owner=self.admin,
        )
        self.login_with_mfa(self.board)
        response = self.client.get(
            reverse("reporting:list_export", args=("incubatees", "xlsx"))
        )
        self.assertEqual(response.status_code, 403)

    def test_xlsx_export_writes_untrusted_formula_values_as_text(self):
        Stakeholder.objects.create(
            name="=HYPERLINK(\"https://example.test\",\"click\")",
            title="+1+1",
            owner=self.admin,
            created_by=self.admin,
        )
        self.login_with_mfa(self.admin)

        response = self.client.get(
            reverse("reporting:list_export", args=("stakeholders", "xlsx"))
        )

        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content), read_only=False)
        worksheet = workbook["Stakeholders"]
        self.assertEqual(worksheet["A2"].value, '=HYPERLINK("https://example.test","click")')
        self.assertEqual(worksheet["A2"].data_type, "s")
        self.assertEqual(worksheet["B2"].value, "+1+1")
        self.assertEqual(worksheet["B2"].data_type, "s")

    def test_audit_events_are_immutable(self):
        event = AuditEvent.objects.create(
            actor=self.admin,
            action=AuditEvent.Action.LOGIN,
            resource="accounts.user",
            object_id=str(self.admin.pk),
        )

        with self.assertRaises(PermissionDenied):
            event.save()
        with self.assertRaises(PermissionDenied):
            AuditEvent.objects.filter(pk=event.pk).update(outcome="tampered")
        with self.assertRaises(PermissionDenied):
            event.delete()
        with self.assertRaises(PermissionDenied):
            AuditEvent.objects.filter(pk=event.pk).delete()

    def test_permission_changes_and_logins_create_audit_events(self):
        incubatee = Incubatee.objects.create(
            legal_name="Synthetic Permission Change Record",
            created_by=self.admin,
        )
        RecordShare.objects.create(
            content_type=ContentType.objects.get_for_model(incubatee),
            object_id=str(incubatee.pk),
            user=self.board,
            actions=["read"],
            created_by=self.admin,
        )
        self.client.force_login(self.admin)

        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditEvent.Action.PERMISSION_CHANGE,
                resource="recordshare",
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditEvent.Action.LOGIN,
                object_id=str(self.admin.pk),
            ).exists()
        )

    def test_sensitive_stakeholder_view_is_audited(self):
        stakeholder = Stakeholder.objects.create(
            name="Synthetic Sensitive Contact",
            email="sensitive-contact@gmail.com",
            created_by=self.admin,
            owner=self.admin,
        )
        self.login_with_mfa(self.admin)

        response = self.client.get(
            reverse("stakeholders:detail", args=(stakeholder.pk,))
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditEvent.Action.SENSITIVE_VIEW,
                resource="stakeholder",
                object_id=str(stakeholder.pk),
            ).exists()
        )

    def test_audit_log_is_visible_to_authorized_reader_only(self):
        event = AuditEvent.objects.create(
            actor=self.admin,
            action=AuditEvent.Action.LOGIN,
            resource="accounts.user",
            object_id=str(self.admin.pk),
        )
        self.client.force_login(self.admin)
        session = self.client.session
        session["mfa_verified_user"] = self.admin.pk
        session.save()
        response = self.client.get(reverse("reporting:audit_log"))
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.system_admin)
        session = self.client.session
        session["mfa_verified_user"] = self.system_admin.pk
        session.save()
        response = self.client.get(reverse("reporting:audit_log"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, event.object_id)

    def test_monthly_summary_task_sends_individual_role_scoped_messages(self):
        mocked_report = {
            "business_count": 5,
            "jobs_created": 3,
            "funding_raised_zar": Decimal("1250.00"),
        }
        with patch("reporting.tasks.impact_report", return_value=mocked_report) as report_mock:
            with patch("reporting.tasks.send_mail", return_value=1) as send_mock:
                result = send_monthly_summary_reports()

        self.assertEqual(result, {"sent": 2, "failed": 0})
        self.assertEqual(report_mock.call_count, 2)
        self.assertEqual(send_mock.call_count, 2)
        recipients = {call.kwargs["recipient_list"][0] for call in send_mock.call_args_list}
        self.assertEqual(
            recipients,
            {self.admin.email, self.board.email},
        )
        for call in report_mock.call_args_list:
            self.assertLessEqual(
                (call.kwargs["date_to"] - call.kwargs["date_from"]).days, 31
            )
