import hashlib
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, TOTPDevice
from incubation.metrics import incubatee_metrics
from incubation.models import (
    CheckInAccessLink,
    CheckInReminder,
    Incubatee,
    IncubateeAccessEvent,
    IncubateeBaselineSnapshot,
    IncubateeExit,
    IncubateeFollowUp,
    IncubateeMetricObservation,
    IncubateeMilestone,
    MentoringSession,
    MonthlyCheckIn,
    MonthlyCheckInEvidence,
    Programme,
    ProgrammeCostObservation,
    QuarterlyReview,
)
from incubation.tasks import (
    create_due_incubatee_followups,
    send_checkin_reminders,
    send_monthly_checkin_links,
)
from stakeholders.models import Stakeholder

User = get_user_model()
TODAY = date(2026, 10, 21)


class PhaseSevenTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.storage_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name),
            STORAGES={
                **settings.STORAGES,
                "default": {
                    "BACKEND": "config.storage.PrivateMediaStorage",
                    "OPTIONS": {"location": self.temp_dir.name},
                },
            },
        )
        self.storage_override.enable()
        self.addCleanup(self.storage_override.disable)
        self.addCleanup(self.temp_dir.cleanup)
        self.admin = User.objects.create_user(
            email="phase7-admin@gmail.com",
            password="synthetic-test-password",
        )
        self.admin.roles.add(Role.objects.get(code=Role.Code.ADMIN))
        TOTPDevice.objects.create(
            user=self.admin,
            secret_encrypted="synthetic-encrypted-test-secret",
            confirmed=True,
        )
        self.client.force_login(self.admin)
        session = self.client.session
        session["mfa_verified_user"] = self.admin.pk
        session.save()

    def create_incubatee(self, *, name="Synthetic Phase 7 Company", status=Incubatee.Status.ACTIVE, **kwargs):
        return Incubatee.objects.create(
            legal_name=name,
            status=status,
            created_by=self.admin,
            updated_by=self.admin,
            owner=self.admin,
            **kwargs,
        )

    def add_founder_email(self, incubatee, email="founder@gmail.com"):
        stakeholder = Stakeholder.objects.create(
            name="Synthetic Founder",
            email=email,
            title="Founder",
            owner=self.admin,
            created_by=self.admin,
        )
        profile = stakeholder.founder_profile if hasattr(stakeholder, "founder_profile") else None
        if profile is None:
            from incubation.models import FounderProfile

            profile = FounderProfile.objects.create(stakeholder=stakeholder)
        from incubation.models import IncubateeFounder

        IncubateeFounder.objects.create(incubatee=incubatee, founder=profile)
        return stakeholder

    @patch("incubation.tasks.timezone.localdate", return_value=TODAY)
    def test_monthly_secure_link_email_idempotence_submission_and_history(self, _today):
        incubatee = self.create_incubatee()
        self.add_founder_email(incubatee)
        with override_settings(
            INCUBATEE_CHECKIN_SEND_DAY=1,
            INCUBATEE_CHECKIN_DEADLINE_DAY=15,
            INCUBATEE_CHECKIN_STATUSES=["active"],
        ):
            sent = send_monthly_checkin_links.apply().get()
            self.assertEqual(sent["emailed"], 1)
            self.assertEqual(send_monthly_checkin_links.apply().get()["emailed"], 0)
        self.assertEqual(len(mail.outbox), 1)
        checkin = MonthlyCheckIn.objects.get(incubatee=incubatee)
        self.assertEqual(checkin.period_start, date(2026, 9, 1))
        self.assertEqual(checkin.due_date, date(2026, 10, 15))
        self.assertEqual(checkin.access_links.count(), 1)

        link = CheckInAccessLink.objects.get(checkin=checkin)
        query = parse_qs(urlsplit(mail.outbox[0].body.splitlines()[2]).query)
        token = query["token"][0]
        self.assertEqual(
            link.token_digest,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn(incubatee.legal_name, mail.outbox[0].body)
        url = reverse("incubation:public_checkin", kwargs={"link_id": link.pk})
        page = self.client.get(f"{url}?token={token}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Cache-Control"], "private, no-store")
        self.assertEqual(page["Referrer-Policy"], "no-referrer")
        self.assertContains(page, "Confidence score")

        statement = SimpleUploadedFile(
            "synthetic-bank-statement.pdf",
            b"%PDF-1.4\nsynthetic bank statement",
            content_type="application/pdf",
        )
        response = self.client.post(
            url,
            {
                "access_token": token,
                "revenue_zar": "0",
                "customer_count_not_applicable": "on",
                "jobs_created": "2",
                "funding_received_zar": "500.25",
                "confidence_score": "8",
                "bank_statement": statement,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        checkin.refresh_from_db()
        self.assertIsNotNone(checkin.submitted_at)
        self.assertEqual(checkin.completeness_score, round(5 * 100 / 14))
        revenue = checkin.observations.get(metric=IncubateeMetricObservation.Metric.REVENUE_ZAR)
        self.assertEqual(revenue.decimal_value, Decimal("0.00"))
        na = checkin.observations.get(metric=IncubateeMetricObservation.Metric.CUSTOMER_COUNT)
        self.assertEqual(na.availability, IncubateeMetricObservation.Availability.NOT_APPLICABLE)
        self.assertIsNone(na.integer_value)
        self.assertEqual(MonthlyCheckInEvidence.objects.get().observation, revenue)
        self.assertTrue(
            IncubateeAccessEvent.objects.filter(
                incubatee=incubatee,
                actor__isnull=True,
                action=IncubateeAccessEvent.Action.CHECKIN_SUBMITTED,
            ).exists()
        )
        self.assertEqual(self.client.post(url, {"access_token": token}).status_code, 409)
        self.assertEqual(checkin.observations.count(), 5)
        evidence_download = self.client.get(
            reverse(
                "incubation:evidence_download",
                args=(incubatee.pk, "checkin", MonthlyCheckInEvidence.objects.get().pk),
            )
        )
        self.assertEqual(evidence_download.status_code, 200)
        evidence_download.close()

    @patch("incubation.tasks.timezone.localdate", return_value=TODAY)
    @override_settings(INCUBATEE_CHECKIN_REMINDER_DAYS=[3])
    def test_configured_reminder_sends_once_and_rotates_link_token(self, _today):
        incubatee = self.create_incubatee()
        checkin = MonthlyCheckIn.objects.create(
            incubatee=incubatee,
            period_start=date(2026, 9, 1),
            due_date=TODAY + timedelta(days=3),
        )
        link = CheckInAccessLink.objects.create(
            checkin=checkin,
            recipient_email="synthetic@gmail.com",
            token_digest="a" * 64,
            expires_at=timezone.now() + timedelta(days=10),
            email_sent_at=timezone.now(),
        )
        result = send_checkin_reminders()
        self.assertEqual(result, {"emailed": 1, "failed": 0})
        self.assertEqual(CheckInReminder.objects.count(), 1)
        link.refresh_from_db()
        self.assertNotEqual(link.token_digest, "a" * 64)
        self.assertEqual(send_checkin_reminders()["emailed"], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_quarterly_review_scores_document_access_and_duplicate_period(self):
        incubatee = self.create_incubatee()
        uploaded = SimpleUploadedFile(
            "synthetic-management-accounts.pdf",
            b"%PDF-1.4\nsynthetic management accounts",
            content_type="application/pdf",
        )
        response = self.client.post(
            reverse("incubation:quarterly_review_add", args=(incubatee.pk,)),
            {
                "period_start": "2026-10-01",
                "finance_score": "4",
                "marketing_score": "3",
                "operations_score": "2",
                "compliance_score": "5",
                "technology_score": "4",
                "leadership_score": "3",
                "tax_clearance_status": "valid",
                "cipc_annual_return_status": "filed",
                "management_accounts": uploaded,
            },
        )
        self.assertEqual(response.status_code, 302)
        review = QuarterlyReview.objects.get(incubatee=incubatee)
        self.assertEqual(review.observations.count(), 8)
        self.assertEqual(review.observations.get(metric="finance_score").integer_value, 4)
        evidence_response = self.client.get(
            reverse(
                "incubation:evidence_download",
                args=(incubatee.pk, "quarterly", review.pk),
            )
        )
        self.assertEqual(evidence_response.status_code, 200)
        evidence_response.close()
        duplicate = self.client.post(
            reverse("incubation:quarterly_review_add", args=(incubatee.pk,)),
            {"period_start": "2026-10-01"},
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, "already exists")
        self.assertEqual(QuarterlyReview.objects.filter(incubatee=incubatee).count(), 1)

    def test_milestones_mentoring_exit_followups_and_metrics(self):
        programme = Programme.objects.create(name="Synthetic Phase 7 programme")
        incubatee = self.create_incubatee(programme=programme)
        IncubateeBaselineSnapshot.objects.create(
            incubatee=incubatee,
            snapshot={
                "monthly_average_revenue_zar": "100.00",
                "full_time_employees": 1,
                "part_time_employees": 0,
                "funding_received_zar": "0.00",
            },
            changed_by=self.admin,
            source="synthetic_baseline",
        )
        milestone = self.client.post(
            reverse("incubation:milestone_add", args=(incubatee.pk,)),
            {
                "milestone_type": IncubateeMilestone.Type.FUNDING_RAISED,
                "milestone_date": "2026-02-01",
                "value_zar": "500.00",
                "description": "Synthetic funding milestone",
            },
        )
        self.assertEqual(milestone.status_code, 302)
        self.assertEqual(IncubateeMilestone.objects.count(), 1)
        mentoring = self.client.post(
            reverse("incubation:mentoring_session_add", args=(incubatee.pk,)),
            {
                "session_date": "2026-02-02",
                "mentoring_hours": "2.50",
                "topics": "Synthetic market planning",
                "progress_rating": "4",
            },
        )
        self.assertEqual(mentoring.status_code, 302)
        session = MentoringSession.objects.get(incubatee=incubatee)
        self.assertEqual(session.mentoring_hours, Decimal("2.50"))

        checkin = MonthlyCheckIn.objects.create(
            incubatee=incubatee,
            period_start=date(2026, 11, 1),
            submitted_at=timezone.now(),
            completeness_score=100,
        )
        IncubateeMetricObservation.objects.create(
            incubatee=incubatee,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            period_start=date(2026, 11, 1),
            metric=IncubateeMetricObservation.Metric.REVENUE_ZAR,
            decimal_value=Decimal("150.00"),
            source="synthetic_checkin",
            monthly_checkin=checkin,
            recorded_by=self.admin,
        )
        IncubateeMetricObservation.objects.create(
            incubatee=incubatee,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            period_start=date(2026, 11, 1),
            metric=IncubateeMetricObservation.Metric.JOBS_CREATED,
            integer_value=2,
            source="synthetic_checkin",
            monthly_checkin=checkin,
            recorded_by=self.admin,
        )
        IncubateeMetricObservation.objects.create(
            incubatee=incubatee,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            period_start=date(2026, 11, 1),
            metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
            decimal_value=Decimal("500.00"),
            source="synthetic_checkin",
            monthly_checkin=checkin,
            recorded_by=self.admin,
        )
        ProgrammeCostObservation.objects.create(
            programme=programme,
            period_start=date(2026, 2, 1),
            amount_zar=Decimal("1000.00"),
            source="synthetic_cost",
            recorded_by=self.admin,
        )
        metrics = incubatee_metrics(incubatee)
        self.assertEqual(metrics["revenue_growth_percent"], Decimal("50.00"))
        self.assertEqual(metrics["jobs_created_since_baseline"], 2)
        self.assertEqual(metrics["funding_received_recorded_zar"], Decimal("500.00"))
        self.assertEqual(metrics["checkin_completion_rate"], Decimal("100"))
        self.assertEqual(metrics["mentoring_hours"], Decimal("2.50"))
        self.assertEqual(metrics["cost_per_job_zar"], Decimal("500"))

        old_exit = IncubateeExit.objects.create(
            incubatee=incubatee,
            exit_date=date(2025, 1, 31),
            outcome=IncubateeExit.Outcome.GRADUATED,
            recorded_by=self.admin,
        )
        with patch("incubation.tasks.timezone.localdate", return_value=TODAY):
            self.assertEqual(create_due_incubatee_followups(), {"created": 2})
        followup = IncubateeFollowUp.objects.get(
            exit_record=old_exit, months_after_exit=12
        )
        self.assertEqual(followup.due_date, date(2026, 1, 31))
        response = self.client.post(
            reverse(
                "incubation:followup_submit",
                args=(incubatee.pk, followup.pk),
            ),
            {
                "business_status": IncubateeFollowUp.Status.OPERATING,
                "revenue_zar": "250.00",
                "full_time_employees": "2",
                "part_time_employees": "0",
                "funding_received_zar": "100.00",
                "notes": "Synthetic follow-up",
            },
        )
        self.assertEqual(response.status_code, 302)
        metrics = incubatee_metrics(incubatee)
        self.assertEqual(metrics["survival_rate_12_months"], Decimal("100"))

    def test_duplicate_metric_and_unauthorized_record_access_are_rejected(self):
        incubatee = self.create_incubatee()
        checkin = MonthlyCheckIn.objects.create(
            incubatee=incubatee,
            period_start=date(2026, 9, 1),
        )
        observation = IncubateeMetricObservation.objects.create(
            incubatee=incubatee,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            period_start=date(2026, 9, 1),
            metric=IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
            integer_value=0,
            source="synthetic",
            monthly_checkin=checkin,
        )
        with self.assertRaises(ValidationError):
            IncubateeMetricObservation.objects.create(
                incubatee=incubatee,
                period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
                period_start=date(2026, 9, 1),
                metric=IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
                integer_value=4,
                source="synthetic",
                monthly_checkin=checkin,
            )
        observation.integer_value = 10
        with self.assertRaises(ValidationError):
            observation.save()
        outsider = User.objects.create_user(
            email="phase7-outsider@gmail.com",
            password="synthetic-test-password",
        )
        self.client.force_login(outsider)
        self.assertEqual(
            self.client.get(reverse("incubation:detail", args=(incubatee.pk,))).status_code,
            403,
        )
