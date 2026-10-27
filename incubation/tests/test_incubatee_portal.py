from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from smtplib import SMTPException
from unittest.mock import patch
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook
from django.utils import timezone

from accounts.models import Role, TOTPDevice
from audit.models import AuditEvent
from incubation.models import (
    BusinessDocument,
    Incubatee,
    IncubateeAccount,
    IncubateeAccountBusiness,
    IncubateeBaselineSnapshot,
    IncubateeBusinessChallenge,
    IncubateeChallengeReview,
    IncubateeBusinessDevelopmentNeed,
    IncubateeBusinessDevelopmentNeedUpdate,
    IncubateeMetricObservation,
    IncubateeMilestone,
    IncubateeMilestoneReview,
    IncubateePortalNotification,
    IncubateeProfileChange,
    IncubateeProgressReview,
    IncubateeProgressSubmission,
    IncubateeSupportRequest,
    IncubateeSupportRequestUpdate,
    MentoringSession,
    MonthlyCheckIn,
)
from interactions.models import Interaction
from stakeholders.models import Stakeholder

User = get_user_model()


class IncubateePortalTests(TestCase):
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

        self.admin = self.create_user("portal-admin@gmail.com", Role.Code.ADMIN)
        self.staff = self.create_user("portal-staff@gmail.com", Role.Code.CFERI_STAFF)
        self.incubatee_user = self.create_user(
            "portal-incubatee@gmail.com", Role.Code.INCUBATEE
        )
        self.business = self.create_business("Portal Business")
        self.other_business = self.create_business("Other Portal Business")
        self.business.assigned_users.add(self.staff)
        self.account = IncubateeAccount.objects.create(
            user=self.incubatee_user,
            entrepreneur_category=IncubateeAccount.EntrepreneurCategory.EXTERNAL_SME,
            created_by=self.admin,
        )
        IncubateeAccountBusiness.objects.create(
            account=self.account,
            incubatee=self.business,
            linked_by=self.admin,
        )

    def create_user(self, email, role_code):
        user = User.objects.create_user(
            email=email,
            password="Portal-Test-Password-2026!",
            first_name="Portal",
            last_name="Tester",
        )
        user.roles.add(Role.objects.get(code=role_code))
        return user

    def create_business(self, name):
        return Incubatee.objects.create(
            legal_name=name,
            cipc_registration_number=f"REG-{name.replace(' ', '-')}",
            entity_type=Incubatee.EntityType.PRIVATE_COMPANY,
            date_registered=date(2024, 1, 10),
            created_by=self.admin,
            updated_by=self.admin,
            owner=self.admin,
        )

    @staticmethod
    def pdf_upload(name="business.pdf", content=b"%PDF-1.4\nsynthetic"):
        return SimpleUploadedFile(name, content, content_type="application/pdf")

    def login(self, user):
        self.client.force_login(user)

    def upload_document(self, *, title="Business plan", file=None, category=None):
        return self.client.post(
            reverse(
                "incubation:business_document_upload",
                args=(self.business.pk,),
            ),
            {
                "title": title,
                "category": category or BusinessDocument.Category.BUSINESS_PLAN,
                "description": "Synthetic business document",
                "expiry_date": "",
                "change_note": "Initial upload",
                "file": file or self.pdf_upload(),
            },
        )

    def test_profile_displays_existing_business_information_and_directly_updates_allowed_fields(self):
        self.business.trading_name = "Portal Trading"
        self.business.sic_code = "1234"
        self.business.business_contact_email = "contact@portal.test"
        self.business.business_contact_number = "+27 12 345 6789"
        self.business.save()
        self.login(self.incubatee_user)

        response = self.client.get(
            reverse("incubation:business_profile", args=(self.business.pk,))
        )
        self.assertContains(response, "Portal Business")
        self.assertContains(response, "REG-Portal-Business")
        self.assertContains(response, "Private company")
        self.assertContains(response, "contact@portal.test")

        updated = self.client.post(
            reverse("incubation:business_profile", args=(self.business.pk,)),
            {
                "form_type": "approved_fields",
                "trading_name": "Updated Trading Name",
                "sic_code": "4321",
                "sub_sector": "Food processing",
                "products_services": "Synthetic products",
                "business_model": Incubatee.BusinessModel.B2B,
                "business_stage": Incubatee.BusinessStage.GROWTH,
                "physical_address": "1 Sample Road",
                "province": "Mpumalanga",
                "municipality": "Mbombela",
                "business_contact_email": "new-contact@portal.test",
                "business_contact_number": "+27 13 987 6543",
            },
            follow=True,
        )

        self.assertRedirects(
            updated,
            reverse("incubation:business_profile", args=(self.business.pk,)),
        )
        self.assertContains(updated, "approved business profile fields were updated")
        self.business.refresh_from_db()
        self.assertEqual(self.business.trading_name, "Updated Trading Name")
        self.assertEqual(self.business.cipc_registration_number, "REG-Portal-Business")
        self.assertEqual(self.business.business_contact_email, "new-contact@portal.test")
        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.incubatee_user,
                action=AuditEvent.Action.UPDATE,
                resource="incubation.business_profile",
            ).exists()
        )

    def test_protected_profile_change_requires_staff_approval_and_retains_history(self):
        self.login(self.incubatee_user)
        proposal = self.client.post(
            reverse("incubation:business_profile", args=(self.business.pk,)),
            {
                "form_type": "protected_fields",
                "legal_name": "Proposed Legal Name",
                "cipc_registration_number": "REG-UPDATED-PORTAL",
                "entity_type": Incubatee.EntityType.COOPERATIVE,
                "date_registered": "2024-02-10",
                "reason": "Corrected legal name supported by company documents.",
            },
        )

        self.assertRedirects(
            proposal,
            reverse("incubation:business_profile", args=(self.business.pk,)),
        )
        self.business.refresh_from_db()
        self.assertEqual(self.business.legal_name, "Portal Business")
        change = IncubateeProfileChange.objects.get()
        self.assertEqual(change.status, IncubateeProfileChange.Status.PENDING)
        self.assertEqual(change.old_values["legal_name"], "Portal Business")

        self.login(self.staff)
        queue = self.client.get(reverse("incubation:profile_change_review_list"))
        self.assertContains(queue, "Proposed Legal Name")
        self.assertContains(queue, "REG-Portal-Business")
        review = self.client.post(
            reverse("incubation:profile_change_review", args=(change.pk,)),
            {"decision": "approve", "review_note": "Evidence verified."},
        )

        self.assertRedirects(review, reverse("incubation:profile_change_review_list"))
        self.business.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(self.business.legal_name, "Proposed Legal Name")
        self.assertEqual(self.business.cipc_registration_number, "REG-UPDATED-PORTAL")
        self.assertEqual(self.business.entity_type, Incubatee.EntityType.COOPERATIVE)
        self.assertEqual(self.business.date_registered, date(2024, 2, 10))
        self.assertEqual(change.status, IncubateeProfileChange.Status.APPROVED)
        self.assertEqual(change.reviewer, self.staff)
        self.assertEqual(change.old_values["legal_name"], "Portal Business")
        self.assertEqual(
            change.old_values["cipc_registration_number"],
            "REG-Portal-Business",
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.staff,
                resource="incubation.profile_change_request",
                metadata__decision="approved",
            ).exists()
        )
        notification = IncubateePortalNotification.objects.get(
            user=self.incubatee_user,
            incubatee=self.business,
            notification_type=IncubateePortalNotification.Type.PROFILE_CHANGE,
        )
        self.assertEqual(notification.title, "Business profile change approved")

    def test_rejected_and_stale_protected_change_requests_are_not_applied(self):
        change = IncubateeProfileChange.objects.create(
            incubatee=self.business,
            requested_by=self.incubatee_user,
            request_note="Correct the registration date.",
            old_values={"date_registered": "2024-01-10"},
            proposed_values={"date_registered": "2024-02-10"},
        )
        self.login(self.staff)
        response = self.client.post(
            reverse("incubation:profile_change_review", args=(change.pk,)),
            {"decision": "reject", "review_note": "Please provide supporting evidence."},
        )
        self.assertEqual(response.status_code, 302)
        self.business.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(self.business.date_registered, date(2024, 1, 10))
        self.assertEqual(change.status, IncubateeProfileChange.Status.REJECTED)

        stale = IncubateeProfileChange.objects.create(
            incubatee=self.business,
            requested_by=self.incubatee_user,
            request_note="Registration correction.",
            old_values={"date_registered": "2024-01-10"},
            proposed_values={"date_registered": "2024-03-10"},
        )
        self.business.date_registered = date(2024, 2, 10)
        self.business.save(update_fields=("date_registered",))
        response = self.client.post(
            reverse("incubation:profile_change_review", args=(stale.pk,)),
            {"decision": "approve"},
        )
        self.assertEqual(response.status_code, 302)
        self.business.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(self.business.date_registered, date(2024, 2, 10))
        self.assertEqual(stale.status, IncubateeProfileChange.Status.STALE)

    def test_incubatee_upload_download_and_version_history_use_private_storage(self):
        self.login(self.incubatee_user)
        uploaded = self.upload_document()
        self.assertRedirects(
            uploaded,
            reverse("incubation:business_document_list", args=(self.business.pk,)),
        )
        document = BusinessDocument.objects.get()
        version_one = document.current_version
        stored_path = Path(settings.PRIVATE_MEDIA_ROOT) / version_one.file.name
        self.assertTrue(stored_path.exists())
        self.assertIn("business-documents", version_one.file.name)
        self.assertEqual(document.status, BusinessDocument.Status.PENDING)
        detail = self.client.get(
            reverse(
                "incubation:business_document_detail",
                args=(self.business.pk, document.pk),
            )
        )
        self.assertContains(detail, "Version history")
        self.assertContains(detail, "Metadata at upload")

        download = self.client.get(
            reverse(
                "incubation:business_document_version_download",
                args=(self.business.pk, document.pk, version_one.pk),
            )
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.4\nsynthetic")
        self.assertEqual(download["Cache-Control"], "private, no-store")
        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.incubatee_user,
                action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
                resource="incubation.business_document",
                version_uuid=version_one.pk,
            ).exists()
        )

        replacement = self.client.post(
            reverse(
                "incubation:business_document_replace",
                args=(self.business.pk, document.pk),
            ),
            {
                "title": "Business plan revised",
                "category": BusinessDocument.Category.BUSINESS_PLAN,
                "description": "Updated plan",
                "expiry_date": "",
                "change_note": "Revised financial assumptions",
                "file": self.pdf_upload("business-revised.pdf", b"%PDF-1.7\nrevised"),
            },
        )
        self.assertRedirects(
            replacement,
            reverse("incubation:business_document_list", args=(self.business.pk,)),
        )
        document.refresh_from_db()
        versions = list(document.versions.order_by("version_number"))
        self.assertEqual(len(versions), 2)
        self.assertFalse(versions[0].is_current)
        self.assertTrue(versions[1].is_current)
        self.assertEqual(versions[0].metadata_snapshot["title"], "Business plan")
        self.assertEqual(versions[1].version_number, 2)
        self.assertEqual(document.status, BusinessDocument.Status.PENDING)
        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.incubatee_user,
                action=AuditEvent.Action.CREATE,
                resource="incubation.business_document",
            ).count()
            >= 2
        )

    def test_document_file_validation_rejects_fake_format_and_over_25mb(self):
        self.login(self.incubatee_user)
        unsupported = self.upload_document(
            file=SimpleUploadedFile("unsupported.txt", b"synthetic")
        )
        self.assertEqual(unsupported.status_code, 200)
        self.assertContains(unsupported, "Upload a PDF, DOCX, XLSX")
        self.assertFalse(BusinessDocument.objects.exists())

        invalid = self.upload_document(
            file=SimpleUploadedFile(
                "not-a-pdf.pdf", b"not a pdf", content_type="application/pdf"
            )
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "file content is not a valid PDF")
        self.assertFalse(BusinessDocument.objects.exists())

        oversized = self.upload_document(
            title="Oversize",
            file=SimpleUploadedFile(
                "oversize.pdf",
                b"%PDF-" + (b"x" * (25 * 1024 * 1024 + 1)),
                content_type="application/pdf",
            ),
        )
        self.assertEqual(oversized.status_code, 200)
        self.assertContains(oversized, "must not exceed 25 MB")
        self.assertFalse(BusinessDocument.objects.exists())

    def test_staff_reviews_documents_only_in_authorized_scope(self):
        self.login(self.incubatee_user)
        self.upload_document()
        document = BusinessDocument.objects.get()
        self.login(self.staff)

        queue = self.client.get(reverse("incubation:business_document_review_list"))
        self.assertContains(queue, "Business plan")
        approved = self.client.post(
            reverse(
                "incubation:business_document_review",
                args=(self.business.pk, document.pk),
            ),
            {"decision": "approve", "review_note": "Document reviewed."},
        )
        self.assertRedirects(
            approved,
            reverse("incubation:business_document_review_list"),
        )
        document.refresh_from_db()
        self.assertEqual(document.status, BusinessDocument.Status.APPROVED)
        self.assertEqual(document.reviewed_by, self.staff)

        self.login(self.incubatee_user)
        self.upload_document(
            title="Proof of address",
            category=BusinessDocument.Category.PROOF_OF_ADDRESS,
        )
        rejected_document = BusinessDocument.objects.get(
            title="Proof of address"
        )
        self.login(self.staff)
        rejected = self.client.post(
            reverse(
                "incubation:business_document_review",
                args=(self.business.pk, rejected_document.pk),
            ),
            {"decision": "reject", "review_note": "Please upload a clearer copy."},
        )
        self.assertEqual(rejected.status_code, 302)
        rejected_document.refresh_from_db()
        self.assertEqual(rejected_document.status, BusinessDocument.Status.REJECTED)
        self.assertEqual(rejected_document.review_note, "Please upload a clearer copy.")
    def test_business_profile_and_documents_are_isolated_from_unlinked_business(self):
        self.login(self.incubatee_user)
        paths = (
            reverse("incubation:business_profile", args=(self.other_business.pk,)),
            reverse("incubation:business_document_list", args=(self.other_business.pk,)),
            reverse("incubation:business_document_upload", args=(self.other_business.pk,)),
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

        self.upload_document()
        document = BusinessDocument.objects.get()
        foreign_business_download = self.client.get(
            reverse(
                "incubation:business_document_download",
                args=(self.other_business.pk, document.pk),
            )
        )
        unlinked_document = BusinessDocument.objects.create(
            incubatee=self.other_business,
            title="Other business document",
            category=BusinessDocument.Category.OTHER,
            uploaded_by=self.admin,
        )
        foreign_document_detail = self.client.get(
            reverse(
                "incubation:business_document_detail",
                args=(self.business.pk, unlinked_document.pk),
            )
        )
        self.assertEqual(foreign_business_download.status_code, 404)
        self.assertEqual(foreign_document_detail.status_code, 404)

    def test_incubatee_cannot_access_staff_review_or_change_its_verified_data(self):
        self.login(self.incubatee_user)
        self.assertEqual(
            self.client.get(reverse("incubation:profile_change_review_list")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("incubation:business_document_review_list")).status_code,
            403,
        )
        response = self.client.post(
            reverse("incubation:business_profile", args=(self.business.pk,)),
            {
                "form_type": "approved_fields",
                "legal_name": "Should Not Work",
                "cipc_registration_number": "CHANGED",
                "entity_type": Incubatee.EntityType.OTHER,
                "trading_name": "Allowed Name",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.business.refresh_from_db()
        self.assertEqual(self.business.legal_name, "Portal Business")
        self.assertEqual(
            self.business.cipc_registration_number,
            "REG-Portal-Business",
        )

    def test_staff_cannot_review_documents_for_unassigned_business(self):
        document = BusinessDocument.objects.create(
            incubatee=self.other_business,
            title="Unassigned document",
            category=BusinessDocument.Category.OTHER,
            uploaded_by=self.incubatee_user,
        )
        self.login(self.staff)
        response = self.client.post(
            reverse(
                "incubation:business_document_review",
                args=(self.other_business.pk, document.pk),
            ),
            {"decision": "approve"},
        )
        self.assertIn(response.status_code, (403, 404))
        document.refresh_from_db()
        self.assertEqual(document.status, BusinessDocument.Status.PENDING)

    def progress_data(self, **overrides):
        data = {
            "reporting_month": str(date.today().month),
            "reporting_year": str(date.today().year),
            "revenue_zar": "12500.50",
            "customer_count": "9",
            "full_time_employees": "3",
            "part_time_employees": "2",
            "full_time_men": "1",
            "full_time_women": "2",
            "full_time_youth": "1",
            "part_time_men": "1",
            "part_time_women": "1",
            "part_time_youth": "1",
            "funding_received_zar": "2500.00",
            "business_achievements": "Launched a new product.",
            "challenges_experienced": "Need additional equipment.",
            "additional_comments": "Synthetic progress entry.",
        }
        data.update(overrides)
        return data

    def test_incubatee_monthly_progress_is_historical_and_uses_existing_metric_records(self):
        baseline = IncubateeBaselineSnapshot.objects.create(
            incubatee=self.business,
            snapshot={"monthly_average_revenue_zar": "7000.00"},
            source="test",
            changed_by=self.admin,
        )
        self.login(self.incubatee_user)

        response = self.client.post(
            reverse("incubation:progress_submit", args=(self.business.pk,)),
            self.progress_data(),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Launched a new product.")
        checkin = MonthlyCheckIn.objects.get(incubatee=self.business)
        submission = IncubateeProgressSubmission.objects.get(checkin=checkin)
        self.assertEqual(checkin.submitted_by, self.incubatee_user)
        self.assertEqual(checkin.submitted_at is not None, True)
        self.assertEqual(
            checkin.observations.get(metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR).decimal_value,
            2500,
        )
        self.assertEqual(checkin.observations.count(), 11)
        self.assertEqual(submission.review_status, "submitted")
        self.assertEqual(self.business.baseline_history.get(pk=baseline.pk).snapshot, baseline.snapshot)
        self.assertEqual(
            AuditEvent.objects.filter(
                resource="incubation.progress_submission",
                object_id=str(submission.pk),
                actor=self.incubatee_user,
            ).count(),
            1,
        )

        duplicate = self.client.post(
            reverse("incubation:progress_submit", args=(self.business.pk,)),
            self.progress_data(),
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(IncubateeProgressSubmission.objects.count(), 1)
        self.login(self.staff)
        review_queue = self.client.get(reverse("incubation:progress_review_list"))
        self.assertContains(review_queue, "12500.50")
        self.assertContains(review_queue, "Funding this period")
        review_response = self.client.post(
            reverse("incubation:progress_review", args=("progress", submission.pk)),
            {
                "status": IncubateeProgressReview.Status.REVIEWED,
                "review_note": "Monthly report checked.",
            },
        )
        self.assertEqual(review_response.status_code, 302)
        self.assertEqual(submission.reviews.count(), 1)
        self.assertEqual(submission.review_status, IncubateeProgressReview.Status.REVIEWED)
        self.assertEqual(
            AuditEvent.objects.filter(
                resource="incubation.progress_review",
                actor=self.staff,
            ).count(),
            1,
        )

    def test_monthly_progress_records_zero_and_leaves_unknown_metrics_absent(self):
        self.login(self.incubatee_user)

        response = self.client.post(
            reverse("incubation:progress_submit", args=(self.business.pk,)),
            self.progress_data(
                revenue_zar="0",
                customer_count="",
                full_time_employees="0",
                part_time_employees="",
                full_time_men="",
                full_time_women="",
                full_time_youth="",
                part_time_men="",
                part_time_women="",
                part_time_youth="",
                funding_received_zar="",
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        observations = IncubateeMetricObservation.objects.filter(
            monthly_checkin__incubatee=self.business
        )
        self.assertEqual(
            observations.get(
                metric=IncubateeMetricObservation.Metric.REVENUE_ZAR
            ).decimal_value,
            Decimal("0"),
        )
        self.assertEqual(
            observations.get(
                metric=IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES
            ).integer_value,
            0,
        )
        self.assertFalse(
            observations.filter(
                metric=IncubateeMetricObservation.Metric.CUSTOMER_COUNT
            ).exists()
        )
        self.assertFalse(
            observations.filter(
                metric=IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES
            ).exists()
        )
        self.assertFalse(
            observations.filter(
                metric=IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR
            ).exists()
        )

    def test_progress_validation_rejects_inconsistent_employment_and_future_period(self):
        self.login(self.incubatee_user)
        invalid_employment = self.client.post(
            reverse("incubation:progress_submit", args=(self.business.pk,)),
            self.progress_data(full_time_men="3", full_time_women="2"),
        )
        self.assertEqual(invalid_employment.status_code, 200)
        self.assertContains(invalid_employment, "cannot exceed the total")
        self.assertFalse(MonthlyCheckIn.objects.exists())

        invalid_period = self.client.post(
            reverse("incubation:progress_submit", args=(self.business.pk,)),
            self.progress_data(
                reporting_month="12",
                reporting_year=str(date.today().year + 1),
            ),
        )
        self.assertEqual(invalid_period.status_code, 200)
        self.assertFalse(MonthlyCheckIn.objects.exists())

    def test_monthly_progress_history_includes_existing_email_checkins(self):
        period = date.today().replace(day=1)
        MonthlyCheckIn.objects.create(
            incubatee=self.business,
            period_start=period,
            submitted_at=timezone.now(),
            submitted_by=self.admin,
        )
        self.login(self.incubatee_user)

        response = self.client.get(
            reverse("incubation:progress_history", args=(self.business.pk,))
        )

        self.assertContains(response, period.strftime("%B %Y"))
        self.assertContains(response, "Submitted")

    def test_incubatee_progress_routes_reject_unlinked_business_identifiers(self):
        self.login(self.incubatee_user)
        routes = (
            ("progress_history", "get"),
            ("progress_submit", "get"),
            ("milestone_list", "get"),
            ("milestone_submit", "post"),
            ("challenge_list", "get"),
            ("challenge_submit", "post"),
            ("support_request_list", "get"),
            ("support_request_submit", "post"),
            ("mentorship_portal", "get"),
            ("notification_centre", "get"),
        )
        for route, method in routes:
            with self.subTest(route=route):
                url = reverse(f"incubation:{route}", args=(self.other_business.pk,))
                response = (
                    self.client.post(url, {})
                    if method == "post"
                    else self.client.get(url)
                )
                self.assertEqual(response.status_code, 404)

    def test_incubatee_support_request_is_scoped_and_staff_can_assign_respond_and_notify(self):
        self.login(self.incubatee_user)
        invalid_upload = self.client.post(
            reverse("incubation:support_request_submit", args=(self.business.pk,)),
            {
                "category": IncubateeSupportRequest.Category.FUNDING_GUIDANCE,
                "description": "Unsupported evidence must not be stored.",
                "evidence": SimpleUploadedFile(
                    "notes.txt", b"not an approved type", content_type="text/plain"
                ),
            },
        )
        self.assertEqual(invalid_upload.status_code, 200)
        self.assertFalse(IncubateeSupportRequest.objects.exists())

        response = self.client.post(
            reverse("incubation:support_request_submit", args=(self.business.pk,)),
            {
                "category": IncubateeSupportRequest.Category.FUNDING_GUIDANCE,
                "description": "Please help review a funding application.",
                "priority": IncubateeSupportRequest.Priority.HIGH,
                "evidence": self.pdf_upload("funding-plan.pdf"),
            },
        )
        self.assertEqual(response.status_code, 302)
        support_request = IncubateeSupportRequest.objects.get()
        self.assertEqual(support_request.status, IncubateeSupportRequest.Status.SUBMITTED)
        self.assertEqual(support_request.requested_by, self.incubatee_user)

        request_list = self.client.get(
            reverse("incubation:support_request_list", args=(self.business.pk,))
        )
        self.assertContains(request_list, "Funding application guidance")
        self.assertContains(request_list, "Submitted")
        evidence = self.client.get(
            reverse(
                "incubation:support_request_evidence_download",
                args=(self.business.pk, support_request.pk),
            )
        )
        self.assertEqual(evidence.status_code, 200)
        evidence.close()
        second_representative = self.create_user(
            "portal-incubatee-second@gmail.com", Role.Code.INCUBATEE
        )
        second_account = IncubateeAccount.objects.create(
            user=second_representative,
            entrepreneur_category=IncubateeAccount.EntrepreneurCategory.EXTERNAL_SME,
            created_by=self.admin,
        )
        IncubateeAccountBusiness.objects.create(
            account=second_account,
            incubatee=self.business,
            linked_by=self.admin,
        )
        self.login(second_representative)
        shared_request_list = self.client.get(
            reverse("incubation:support_request_list", args=(self.business.pk,))
        )
        self.assertContains(shared_request_list, "Please help review a funding application.")
        shared_evidence = self.client.get(
            reverse(
                "incubation:support_request_evidence_download",
                args=(self.business.pk, support_request.pk),
            )
        )
        self.assertEqual(shared_evidence.status_code, 200)
        shared_evidence.close()

        self.login(self.incubatee_user)
        foreign_evidence = self.client.get(
            reverse(
                "incubation:support_request_evidence_download",
                args=(self.other_business.pk, support_request.pk),
            )
        )
        self.assertEqual(foreign_evidence.status_code, 404)

        self.login(self.staff)
        review_list = self.client.get(
            reverse("incubation:support_request_review_list")
        )
        self.assertContains(review_list, self.business.legal_name)
        self.assertContains(review_list, "Assign and respond")
        reviewed = self.client.post(
            reverse(
                "incubation:support_request_review",
                args=(support_request.pk,),
            ),
            {
                "status": IncubateeSupportRequest.Status.UNDER_REVIEW,
                "assigned_to": str(self.staff.pk),
                "response": "A CFERI advisor will contact you.",
            },
        )
        self.assertEqual(reviewed.status_code, 302)
        support_request.refresh_from_db()
        self.assertEqual(support_request.assigned_to, self.staff)
        self.assertEqual(support_request.status, IncubateeSupportRequest.Status.UNDER_REVIEW)
        self.assertEqual(support_request.updates.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(
                resource="incubation.support_request",
                actor=self.staff,
                object_id=str(support_request.pk),
            ).count(),
            1,
        )

        self.login(self.incubatee_user)
        notifications = self.client.get(
            reverse("incubation:notification_centre", args=(self.business.pk,))
        )
        self.assertContains(notifications, "Support request update")
        self.assertContains(notifications, "A CFERI staff member updated your support request.")
        notification = IncubateePortalNotification.objects.get(user=self.incubatee_user)
        self.assertIsNotNone(notification.email_sent_at)
        self.assertEqual(mail.outbox[-1].to, [self.incubatee_user.email])
        self.assertIn("A CFERI staff member updated your support request.", mail.outbox[-1].body)
        marked_read = self.client.post(
            reverse(
                "incubation:notification_mark_read",
                args=(self.business.pk, notification.pk),
            )
        )
        self.assertEqual(marked_read.status_code, 302)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

    def test_support_request_staff_review_cannot_cross_business_scope(self):
        support_request = IncubateeSupportRequest.objects.create(
            incubatee=self.other_business,
            requested_by=self.incubatee_user,
            category=IncubateeSupportRequest.Category.MENTORSHIP,
            description="Synthetic unrelated business request.",
        )
        self.login(self.staff)
        response = self.client.post(
            reverse(
                "incubation:support_request_review",
                args=(support_request.pk,),
            ),
            {
                "status": IncubateeSupportRequest.Status.IN_PROGRESS,
                "assigned_to": str(self.staff.pk),
                "response": "Not authorized.",
            },
        )
        self.assertEqual(response.status_code, 404)
        support_request.refresh_from_db()
        self.assertEqual(support_request.status, IncubateeSupportRequest.Status.SUBMITTED)
        self.assertFalse(support_request.updates.exists())

    @patch(
        "incubation.portal_notifications.send_mail",
        side_effect=SMTPException("mail delivery unavailable"),
    )
    def test_failed_support_update_email_is_recorded_without_losing_portal_notification(
        self, _send_mail
    ):
        support_request = IncubateeSupportRequest.objects.create(
            incubatee=self.business,
            requested_by=self.incubatee_user,
            category=IncubateeSupportRequest.Category.MENTORSHIP,
            description="Synthetic request for email failure handling.",
        )
        self.login(self.staff)

        response = self.client.post(
            reverse(
                "incubation:support_request_review",
                args=(support_request.pk,),
            ),
            {
                "status": IncubateeSupportRequest.Status.UNDER_REVIEW,
                "assigned_to": str(self.staff.pk),
                "response": "Your request is being reviewed.",
            },
        )

        self.assertEqual(response.status_code, 302)
        notification = IncubateePortalNotification.objects.get(
            user=self.incubatee_user
        )
        self.assertIsNotNone(notification.email_attempted_at)
        self.assertIsNone(notification.email_sent_at)
        self.assertIn("mail delivery unavailable", notification.email_error)
        self.login(self.incubatee_user)
        centre = self.client.get(
            reverse("incubation:notification_centre", args=(self.business.pk,))
        )
        self.assertContains(centre, "Email delivery failed")
        self.assertContains(centre, "Support request update")

    def test_mentorship_page_shows_assigned_mentor_and_safe_business_sessions(self):
        mentor = Stakeholder.objects.create(
            name="Synthetic assigned mentor",
            title="Business advisor",
            created_by=self.admin,
            owner=self.admin,
        )
        self.business.mentors.add(mentor)
        session = MentoringSession.objects.create(
            incubatee=self.business,
            mentor=mentor,
            session_date=date.today(),
            mentoring_hours="1.50",
            topics="Cashflow planning",
            progress_rating="4.00",
            recorded_by=self.admin,
        )
        interaction = Interaction.objects.create(
            occurred_at=timezone.now(),
            duration_minutes=45,
            interaction_type=Interaction.Type.MENTORING,
            topics="Confidential internal notes",
            outcome="Confidential outcome",
            created_by=self.admin,
            owner=self.admin,
            sensitivity=Interaction.Sensitivity.PUBLIC,
        )
        interaction.incubatees.add(self.business)
        self.login(self.incubatee_user)

        response = self.client.get(
            reverse("incubation:mentorship_portal", args=(self.business.pk,))
        )

        self.assertContains(response, "Synthetic assigned mentor")
        self.assertContains(response, "Cashflow planning")
        self.assertContains(response, "Scheduled")
        self.assertContains(response, "Mentoring interaction")
        self.assertNotContains(response, "Confidential internal notes")
        self.assertNotContains(response, "Confidential outcome")
        self.assertEqual(MentoringSession.objects.filter(incubatee=self.business).count(), 1)

    def test_business_development_needs_create_multiple_records_and_link_support_requests(self):
        self.login(self.incubatee_user)
        invalid_evidence = self.client.post(
            reverse(
                "incubation:business_development_needs", args=(self.business.pk,)
            ),
            {
                "categories": [IncubateeBusinessDevelopmentNeed.Category.FUNDING],
                "description": "Need grant application support.",
                "evidence": SimpleUploadedFile(
                    "invalid.txt", b"unsupported", content_type="text/plain"
                ),
            },
        )
        self.assertEqual(invalid_evidence.status_code, 200)
        self.assertFalse(
            IncubateeBusinessDevelopmentNeed.objects.filter(
                incubatee=self.business
            ).exists()
        )

        response = self.client.post(
            reverse(
                "incubation:business_development_needs", args=(self.business.pk,)
            ),
            {
                "categories": [
                    IncubateeBusinessDevelopmentNeed.Category.FUNDING,
                    IncubateeBusinessDevelopmentNeed.Category.MARKET_ACCESS,
                    IncubateeBusinessDevelopmentNeed.Category.OTHER,
                ],
                "description": "Need help to prepare for business growth.",
                "create_support_requests": "on",
                "evidence": self.pdf_upload("needs.pdf"),
            },
        )

        self.assertEqual(response.status_code, 302)
        needs = IncubateeBusinessDevelopmentNeed.objects.filter(
            incubatee=self.business
        ).order_by("category")
        self.assertEqual(needs.count(), 3)
        self.assertEqual(
            IncubateeBusinessDevelopmentNeedUpdate.objects.filter(
                need__incubatee=self.business
            ).count(),
            3,
        )
        self.assertEqual(
            IncubateeSupportRequest.objects.filter(incubatee=self.business).count(), 3
        )
        for need in needs:
            self.assertEqual(need.status, IncubateeBusinessDevelopmentNeed.Status.OPEN)
            self.assertEqual(need.description, "Need help to prepare for business growth.")
            self.assertTrue(need.support_request_id)
            self.assertGreater(need.evidence.size, 0)
        self.assertEqual(
            AuditEvent.objects.filter(
                resource="incubation.business_development_need",
                action=AuditEvent.Action.CREATE,
                actor=self.incubatee_user,
            ).count(),
            3,
        )

        dashboard = self.client.get(reverse("incubation:incubatee_dashboard"))
        self.assertContains(dashboard, "My Business Development Needs")
        self.assertContains(dashboard, "3 outstanding needs")

    def test_staff_can_record_assistance_and_resolve_need_with_linked_activities(self):
        need = IncubateeBusinessDevelopmentNeed.objects.create(
            incubatee=self.business,
            requested_by=self.incubatee_user,
            category=IncubateeBusinessDevelopmentNeed.Category.MENTORSHIP,
            description="Need help with financial planning.",
        )
        IncubateeBusinessDevelopmentNeedUpdate.objects.create(
            need=need,
            status=need.status,
            updated_by=self.incubatee_user,
        )
        session = MentoringSession.objects.create(
            incubatee=self.business,
            session_date=timezone.localdate(),
            mentoring_hours="1.00",
            topics="Financial planning",
            recorded_by=self.admin,
        )
        interaction = Interaction.objects.create(
            occurred_at=timezone.now(),
            duration_minutes=60,
            interaction_type=Interaction.Type.WORKSHOP,
            created_by=self.admin,
            owner=self.admin,
            sensitivity=Interaction.Sensitivity.PUBLIC,
        )
        interaction.incubatees.add(self.business)
        interaction.assigned_users.add(self.staff)
        self.login(self.staff)

        response = self.client.post(
            reverse("incubation:staff_business_development_need_update", args=(need.pk,)),
            {
                "status": IncubateeBusinessDevelopmentNeed.Status.RESOLVED,
                "assigned_to": self.staff.pk,
                "support_provided": "Provided one-to-one financial planning mentorship.",
                "outcome": "A cash-flow plan was completed.",
                "resolution_date": "",
                "mentoring_session": session.pk,
                "interaction": interaction.pk,
                "evidence": self.pdf_upload("assistance.pdf"),
            },
        )

        self.assertEqual(response.status_code, 302)
        need.refresh_from_db()
        self.assertEqual(need.status, IncubateeBusinessDevelopmentNeed.Status.RESOLVED)
        self.assertEqual(need.assigned_to, self.staff)
        self.assertEqual(need.resolution_date, timezone.localdate())
        self.assertEqual(need.updates.count(), 2)
        update = need.updates.first()
        self.assertEqual(update.support_provided, "Provided one-to-one financial planning mentorship.")
        self.assertEqual(update.outcome, "A cash-flow plan was completed.")
        self.assertEqual(update.mentoring_session, session)
        self.assertEqual(update.interaction, interaction)
        self.assertGreater(update.evidence.size, 0)
        with self.assertRaises(PermissionDenied):
            need.updates.update(outcome="Changed history")
        with self.assertRaises(PermissionDenied):
            need.updates.all().delete()
        self.assertTrue(
            AuditEvent.objects.filter(
                resource="incubation.business_development_need",
                action=AuditEvent.Action.UPDATE,
                actor=self.staff,
            ).exists()
        )

        self.login(self.incubatee_user)
        new_need = self.client.post(
            reverse(
                "incubation:business_development_needs", args=(self.business.pk,)
            ),
            {
                "categories": [IncubateeBusinessDevelopmentNeed.Category.MENTORSHIP],
                "description": "Need a follow-up session.",
            },
        )
        self.assertEqual(new_need.status_code, 302)
        self.assertEqual(
            IncubateeBusinessDevelopmentNeed.objects.filter(
                incubatee=self.business,
                category=IncubateeBusinessDevelopmentNeed.Category.MENTORSHIP,
            ).count(),
            2,
        )
        need.refresh_from_db()
        self.assertEqual(need.status, IncubateeBusinessDevelopmentNeed.Status.RESOLVED)
        self.assertEqual(need.updates.count(), 2)

        self.login(self.staff)
        staff_dashboard = self.client.get(
            reverse("incubation:staff_incubatee_monitoring")
        )
        self.assertContains(staff_dashboard, "Outstanding business development needs")
        self.assertContains(staff_dashboard, "1</strong>")

    def test_business_development_need_records_are_business_scoped_and_staff_assigned(self):
        foreign_need = IncubateeBusinessDevelopmentNeed.objects.create(
            incubatee=self.other_business,
            requested_by=self.incubatee_user,
            category=IncubateeBusinessDevelopmentNeed.Category.FUNDING,
        )
        self.login(self.incubatee_user)
        foreign_portal = self.client.get(
            reverse(
                "incubation:business_development_needs",
                args=(self.other_business.pk,),
            )
        )
        self.assertEqual(foreign_portal.status_code, 404)

        self.login(self.staff)
        queue = self.client.get(
            reverse("incubation:staff_business_development_needs")
        )
        self.assertNotContains(queue, "Other Portal Business")
        foreign_update = self.client.post(
            reverse(
                "incubation:staff_business_development_need_update",
                args=(foreign_need.pk,),
            ),
            {
                "status": IncubateeBusinessDevelopmentNeed.Status.IN_PROGRESS,
                "assigned_to": "",
                "support_provided": "",
                "outcome": "",
                "resolution_date": "",
                "mentoring_session": "",
                "interaction": "",
                "evidence": "",
            },
        )
        self.assertEqual(foreign_update.status_code, 404)

        self.login(self.incubatee_user)
        forbidden_queue = self.client.get(
            reverse("incubation:staff_business_development_needs")
        )
        self.assertEqual(forbidden_queue.status_code, 403)

    def test_notification_centre_uses_existing_due_dates_and_business_document_expiry(self):
        today = timezone.localdate()
        MonthlyCheckIn.objects.create(
            incubatee=self.business,
            period_start=today.replace(day=1),
            due_date=today + timedelta(days=3),
            sent_at=timezone.now(),
        )
        BusinessDocument.objects.create(
            incubatee=self.business,
            title="Confidential customer account certificate",
            category=BusinessDocument.Category.CIPC_REGISTRATION,
            status=BusinessDocument.Status.APPROVED,
            expiry_date=today + timedelta(days=20),
            uploaded_by=self.incubatee_user,
        )
        self.login(self.incubatee_user)

        response = self.client.get(
            reverse("incubation:notification_centre", args=(self.business.pk,))
        )

        self.assertContains(response, "Monthly progress is due")
        self.assertContains(response, "CIPC registration document")
        self.assertNotContains(response, "Confidential customer account certificate")
        self.assertNotContains(response, "60-day expiry window")

    def test_milestones_challenges_and_evidence_are_private_and_staff_reviewable(self):
        self.business.assigned_users.add(self.staff)
        self.login(self.incubatee_user)
        milestone_response = self.client.post(
            reverse("incubation:milestone_submit", args=(self.business.pk,)),
            {
                "title": "First customer",
                "milestone_type": IncubateeMilestone.Type.FIRST_SALE,
                "description": "First customer order confirmed.",
                "achieved_on": date.today().isoformat(),
                "evidence": self.pdf_upload("order.pdf"),
            },
        )
        self.assertEqual(milestone_response.status_code, 302)
        milestone = IncubateeMilestone.objects.get(title="First customer")
        self.assertEqual(milestone.review_status, "pending")

        invalid_challenge = self.client.post(
            reverse("incubation:challenge_submit", args=(self.business.pk,)),
            {
                "category": IncubateeBusinessChallenge.Category.EQUIPMENT,
                "description": "Synthetic invalid evidence test.",
                "evidence": SimpleUploadedFile(
                    "unsupported.txt", b"not an approved file", content_type="text/plain"
                ),
            },
        )
        self.assertEqual(invalid_challenge.status_code, 200)
        self.assertFalse(IncubateeBusinessChallenge.objects.exists())

        challenge_response = self.client.post(
            reverse("incubation:challenge_submit", args=(self.business.pk,)),
            {
                "category": IncubateeBusinessChallenge.Category.EQUIPMENT,
                "description": "Production equipment is unavailable.",
                "evidence": self.pdf_upload("quote.pdf"),
            },
        )
        self.assertEqual(challenge_response.status_code, 302)
        challenge = IncubateeBusinessChallenge.objects.get()
        challenge_download = self.client.get(
            reverse(
                "incubation:progress_evidence_download",
                args=(self.business.pk, "challenge", challenge.pk),
            )
        )
        self.assertEqual(challenge_download.status_code, 200)
        challenge_download.close()
        foreign_evidence = self.client.get(
            reverse(
                "incubation:progress_evidence_download",
                args=(self.other_business.pk, "challenge", challenge.pk),
            )
        )
        self.assertEqual(foreign_evidence.status_code, 404)
        self.login(self.staff)

        queue = self.client.get(reverse("incubation:progress_review_list"))
        self.assertContains(queue, "First customer")
        self.assertContains(queue, "Production equipment is unavailable.")
        milestone_download = self.client.get(
            reverse(
                "incubation:progress_evidence_download",
                args=(self.business.pk, "milestone", milestone.pk),
            )
        )
        self.assertEqual(milestone_download.status_code, 200)
        milestone_download.close()

        review = self.client.post(
            reverse("incubation:progress_review", args=("milestone", milestone.pk)),
            {
                "status": IncubateeMilestoneReview.Status.APPROVED,
                "review_note": "Evidence checked.",
            },
        )
        self.assertEqual(review.status_code, 302)
        self.assertEqual(milestone.reviews.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(
                resource="incubation.milestone_review",
                actor=self.staff,
            ).count(),
            1,
        )

        challenge_review = self.client.post(
            reverse("incubation:progress_review", args=("challenge", challenge.pk)),
            {
                "status": IncubateeChallengeReview.Status.IN_PROGRESS,
                "review_note": "Referred for equipment support.",
            },
        )
        self.assertEqual(challenge_review.status_code, 302)
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, IncubateeBusinessChallenge.Status.IN_PROGRESS)

    def test_staff_review_scope_and_incubatee_review_access_are_enforced(self):
        self.login(self.incubatee_user)
        self.client.post(
            reverse("incubation:milestone_submit", args=(self.business.pk,)),
            {
                "title": "Registration completed",
                "milestone_type": IncubateeMilestone.Type.BUSINESS_REGISTRATION,
                "description": "Business registration completed.",
                "achieved_on": date.today().isoformat(),
            },
        )
        milestone = IncubateeMilestone.objects.get()
        denied_queue = self.client.get(reverse("incubation:progress_review_list"))
        self.assertEqual(denied_queue.status_code, 403)
        self.business.assigned_users.remove(self.staff)
        self.login(self.staff)

        unassigned_review = self.client.post(
            reverse("incubation:progress_review", args=("milestone", milestone.pk)),
            {"status": IncubateeMilestoneReview.Status.APPROVED},
        )
        self.assertIn(unassigned_review.status_code, (403, 404))
        self.assertFalse(IncubateeMilestoneReview.objects.exists())

    def test_my_reports_exports_are_business_scoped_and_audited(self):
        period = date.today().replace(day=1)
        for metric, value in (
            (IncubateeMetricObservation.Metric.REVENUE_ZAR, {"decimal_value": "4820.50"}),
            (IncubateeMetricObservation.Metric.CUSTOMER_COUNT, {"integer_value": 12}),
            (IncubateeMetricObservation.Metric.NEW_CUSTOMERS, {"integer_value": 4}),
            (IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES, {"integer_value": 3}),
            (IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES, {"integer_value": 1}),
            (IncubateeMetricObservation.Metric.FULL_TIME_MEN, {"integer_value": 2}),
            (IncubateeMetricObservation.Metric.FULL_TIME_WOMEN, {"integer_value": 1}),
            (IncubateeMetricObservation.Metric.FULL_TIME_YOUTH, {"integer_value": 1}),
            (IncubateeMetricObservation.Metric.PART_TIME_MEN, {"integer_value": 1}),
            (IncubateeMetricObservation.Metric.JOBS_CREATED, {"integer_value": 2}),
            (IncubateeMetricObservation.Metric.FUNDING_APPLIED_ZAR, {"decimal_value": "10000.00"}),
            (IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR, {"decimal_value": "2500.00"}),
        ):
            IncubateeMetricObservation.objects.create(
                incubatee=self.business,
                period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
                period_start=period,
                metric=metric,
                source="test",
                **value,
            )
        checkin = MonthlyCheckIn.objects.create(
            incubatee=self.business,
            period_start=period,
            submitted_at=timezone.now(),
            submitted_by=self.incubatee_user,
        )
        IncubateeMilestone.objects.create(
            title="First customer",
            incubatee=self.business,
            milestone_type=IncubateeMilestone.Type.FIRST_CUSTOMER,
            milestone_date=period,
            recorded_by=self.incubatee_user,
        )
        IncubateeBusinessChallenge.objects.create(
            incubatee=self.business,
            category=IncubateeBusinessChallenge.Category.MARKETING,
            description="Needs a wider customer reach.",
            reported_by=self.incubatee_user,
        )
        self.login(self.incubatee_user)

        report = self.client.get(
            reverse("incubation:incubatee_reports", args=(self.business.pk,))
        )
        excel = self.client.get(
            reverse(
                "incubation:incubatee_report_export",
                args=(self.business.pk, "xlsx"),
            )
        )
        pdf = self.client.get(
            reverse(
                "incubation:incubatee_report_export",
                args=(self.business.pk, "pdf"),
            )
        )
        tampered = self.client.get(
            reverse("incubation:incubatee_reports", args=(self.other_business.pk,))
        )

        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "4820.50")
        self.assertContains(report, "Latest monthly revenue")
        self.assertContains(report, "Latest customers")
        self.assertContains(report, "Latest reported new customers")
        self.assertContains(report, "Employment")
        self.assertContains(report, "Latest reported jobs created")
        self.assertContains(report, "Latest monthly funding received")
        self.assertContains(report, "12")
        self.assertContains(report, "4")
        self.assertContains(report, "Customers")
        self.assertContains(report, "New customers")
        self.assertContains(report, "Full-time men")
        self.assertContains(report, "Part-time men")
        self.assertContains(report, "Jobs created")
        self.assertContains(report, "Funding applied for (ZAR)")
        self.assertContains(report, "Funding received this period (ZAR)")
        self.assertContains(report, "10000.00")
        self.assertContains(report, "2500.00")
        self.assertContains(report, "First customer")
        self.assertContains(report, "Needs a wider customer reach.")
        self.assertEqual(excel.status_code, 200)
        self.assertIn("spreadsheetml", excel["Content-Type"])
        excel.close()
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        pdf.close()
        self.assertEqual(tampered.status_code, 404)
        self.assertEqual(
            AuditEvent.objects.filter(
                actor=self.incubatee_user,
                resource="incubation.incubatee_report",
                action=AuditEvent.Action.EXPORT,
            ).count(),
            2,
        )
        self.assertTrue(MonthlyCheckIn.objects.filter(pk=checkin.pk).exists())

    def test_staff_admin_and_super_admin_can_open_reports_with_role_scope(self):
        report_url = reverse("incubation:incubatee_reports", args=(self.business.pk,))
        list_url = reverse("incubation:list")

        self.login(self.staff)
        staff_list = self.client.get(list_url)
        self.assertNotContains(staff_list, self.business.legal_name)
        self.assertContains(staff_list, "Enter a business name")
        staff_list = self.client.get(list_url, {"q": self.business.legal_name})
        self.assertContains(staff_list, report_url)
        self.assertContains(staff_list, self.business.legal_name)
        self.assertNotContains(staff_list, self.other_business.legal_name)
        self.assertEqual(self.client.get(report_url).status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse("incubation:incubatee_reports", args=(self.other_business.pk,))
            ).status_code,
            404,
        )

        self.login(self.admin)
        admin_list = self.client.get(list_url)
        self.assertNotContains(admin_list, self.business.legal_name)
        admin_list = self.client.get(list_url, {"q": self.business.legal_name})
        self.assertContains(admin_list, report_url)
        self.assertEqual(self.client.get(report_url).status_code, 200)

        super_admin = self.create_user(
            "portal-super-admin@gmail.com", Role.Code.SUPER_ADMIN
        )
        TOTPDevice.objects.create(
            user=super_admin,
            secret_encrypted="synthetic-encrypted-test-secret",
            confirmed=True,
        )
        self.login(super_admin)
        session = self.client.session
        session["mfa_verified_user"] = super_admin.pk
        session.save()
        super_admin_list = self.client.get(list_url)
        self.assertNotContains(super_admin_list, self.business.legal_name)
        super_admin_list = self.client.get(
            list_url, {"q": self.business.legal_name}
        )
        self.assertContains(super_admin_list, report_url)
        self.assertEqual(self.client.get(report_url).status_code, 200)

    def test_my_reports_distinguish_missing_metrics_from_recorded_zero(self):
        self.login(self.incubatee_user)

        report = self.client.get(
            reverse("incubation:incubatee_reports", args=(self.business.pk,))
        )

        self.assertEqual(report.status_code, 200)
        self.assertNotContains(report, "Not recorded")
        self.assertContains(report, "Latest monthly revenue")
        self.assertNotContains(report, "Not provided")
        self.assertContains(report, "—")
        self.assertNotContains(report, "FT 0 / PT 0")

    def test_report_preserves_onboarding_baseline_and_exports_numeric_funding_totals(self):
        older_snapshot = {
            "customer_count": 88,
            "monthly_average_revenue_zar": "800.00",
            "full_time_employees": 8,
            "part_time_employees": 8,
            "funding_received_zar": "90000.00",
            "top_support_needs": [],
        }
        IncubateeBaselineSnapshot.objects.create(
            incubatee=self.business,
            snapshot=older_snapshot,
            source="manual",
            changed_by=self.admin,
        )
        IncubateeBaselineSnapshot.objects.create(
            incubatee=self.business,
            snapshot={
                "business_stage": Incubatee.BusinessStage.IDEA,
                "business_registration_status": Incubatee.RegistrationStatus.NOT_REGISTERED,
                "customer_count": 0,
                "monthly_average_revenue_zar": "0.00",
                "full_time_employees": 0,
                "part_time_employees": 0,
                "funding_received_zar": "1000.00",
                "top_support_needs": [],
            },
            source="initial_onboarding_baseline",
            changed_by=self.admin,
        )
        period = date.today().replace(day=1)
        for metric, value in (
            (IncubateeMetricObservation.Metric.REVENUE_ZAR, {"decimal_value": "0.00"}),
            (IncubateeMetricObservation.Metric.CUSTOMER_COUNT, {"integer_value": 4}),
            (IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES, {"integer_value": 1}),
            (IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES, {"integer_value": 2}),
            (IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR, {"decimal_value": "2500.00"}),
        ):
            IncubateeMetricObservation.objects.create(
                incubatee=self.business,
                period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
                period_start=period,
                metric=metric,
                source="test",
                **value,
            )
        self.login(self.incubatee_user)

        report = self.client.get(
            reverse("incubation:incubatee_reports", args=(self.business.pk,))
        )
        excel = self.client.get(
            reverse(
                "incubation:incubatee_report_export",
                args=(self.business.pk, "xlsx"),
            )
        )

        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.context["baseline_metrics"]["customer_count"], 0)
        self.assertEqual(report.context["latest_values"]["customer_count"], 4)
        self.assertEqual(report.context["cumulative_funding"], Decimal("3500.00"))
        self.assertContains(report, "R 0.00")
        workbook = load_workbook(BytesIO(excel.content), read_only=True, data_only=True)
        summary = {
            row[0]: row[1]
            for row in workbook["Business summary"].iter_rows(
                min_row=2, values_only=True
            )
        }
        self.assertEqual(summary["Initial customers"], 0)
        self.assertEqual(summary["Latest customers"], 4)
        self.assertEqual(summary["Reported cumulative funding (ZAR)"], 3500)

    def test_staff_monitoring_and_incubatee_profile_use_authorized_business_scope(self):
        period = date.today().replace(day=1)
        MonthlyCheckIn.objects.create(
            incubatee=self.business,
            period_start=period,
            due_date=date.today() + timedelta(days=3),
        )
        BusinessDocument.objects.create(
            incubatee=self.business,
            title="Pending business plan",
            category=BusinessDocument.Category.BUSINESS_PLAN,
            uploaded_by=self.incubatee_user,
        )
        linked_account = IncubateeAccount.objects.get(user=self.incubatee_user)
        linked_account.entrepreneur_category = (
            IncubateeAccount.EntrepreneurCategory.STUDENT_ENTREPRENEUR
        )
        linked_account.save(update_fields=("entrepreneur_category",))
        self.login(self.staff)

        dashboard = self.client.get(reverse("reporting:cferi_staff"))
        monitoring = self.client.get(
            reverse("incubation:staff_incubatee_monitoring")
        )
        filtered = self.client.get(
            reverse("incubation:staff_incubatee_monitoring"),
            {"business": str(self.other_business.pk)},
        )
        profile = self.client.get(
            reverse("incubation:account_profile", args=(linked_account.pk,))
        )

        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, "Monitoring data is limited")
        self.assertContains(monitoring, "Incubatee Monitoring")
        self.assertContains(monitoring, "Registered incubatee accounts")
        self.assertContains(monitoring, "Pending business plan")
        self.assertNotContains(monitoring, "Synthetic Unassigned Business")
        self.assertEqual(filtered.status_code, 200)
        self.assertNotContains(filtered, "Pending business plan")
        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, "Portal Tester")
        self.assertContains(profile, "Portal Business")
        self.assertContains(profile, "Pending business plan")
        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.staff,
                resource="incubation.incubatee_account_profile",
                action=AuditEvent.Action.SENSITIVE_VIEW,
            ).exists()
        )

    def test_staff_impact_summary_uses_latest_stocks_and_selected_month_funding_flow(self):
        current_period = date.today().replace(day=1)
        previous_period = (current_period - timedelta(days=1)).replace(day=1)
        self.business.status = Incubatee.Status.ACTIVE
        self.business.business_stage = Incubatee.BusinessStage.IDEA
        self.business.business_registration_status = (
            Incubatee.RegistrationStatus.NOT_REGISTERED
        )
        self.business.save(
            update_fields=(
                "status",
                "business_stage",
                "business_registration_status",
                "updated_at",
            )
        )
        IncubateeBaselineSnapshot.objects.create(
            incubatee=self.business,
            snapshot={
                "full_time_employees": 2,
                "part_time_employees": 1,
            },
            source="staff_incubatee_registration",
            changed_by=self.admin,
        )
        observations = (
            (
                previous_period,
                IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
                {"integer_value": 5},
            ),
            (
                previous_period,
                IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES,
                {"integer_value": 2},
            ),
            (
                previous_period,
                IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES,
                {"integer_value": 1},
            ),
            (
                previous_period,
                IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
                {"decimal_value": "900.00"},
            ),
            (
                current_period,
                IncubateeMetricObservation.Metric.CUSTOMER_COUNT,
                {"integer_value": 9},
            ),
            (
                current_period,
                IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES,
                {"integer_value": 3},
            ),
            (
                current_period,
                IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES,
                {"integer_value": 2},
            ),
            (
                current_period,
                IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR,
                {"decimal_value": "100.00"},
            ),
        )
        for observation_period, metric, value in observations:
            IncubateeMetricObservation.objects.create(
                incubatee=self.business,
                period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
                period_start=observation_period,
                metric=metric,
                source="test",
                **value,
            )
        MonthlyCheckIn.objects.create(
            incubatee=self.business,
            period_start=current_period,
            submitted_at=timezone.now(),
            submitted_by=self.incubatee_user,
        )
        MentoringSession.objects.create(
            incubatee=self.business,
            session_date=current_period,
            mentoring_hours=Decimal("1.50"),
            topics="Synthetic planning session",
            recorded_by=self.staff,
        )
        self.login(self.staff)

        response = self.client.get(
            reverse("incubation:staff_incubatee_monitoring"),
            {"reporting_period": current_period.strftime("%Y-%m")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["monitoring_total_customers"], 9)
        self.assertEqual(response.context["monitoring_customer_business_count"], 1)
        self.assertEqual(response.context["monitoring_employment_change"], 2)
        self.assertEqual(response.context["monitoring_funding_total"], Decimal("100.00"))
        self.assertEqual(response.context["monitoring_progress_completion_rate"], 100)
        self.assertEqual(response.context["monitoring_mentoring_session_count"], 1)

    def test_incubatee_onboarding_and_reporting_journey_end_to_end(self):
        self.login(self.staff)
        registration = self.client.post(
            reverse("incubation:account_create"),
            {
                "registration_mode": "existing_business",
                "full_name": "Journey Representative",
                "email": "journey.rep@gmail.com",
                "businesses": [str(self.business.pk)],
                "entrepreneur_category": IncubateeAccount.EntrepreneurCategory.EXTERNAL_SME,
                "account_status": "active",
            },
        )
        self.assertEqual(registration.status_code, 302)
        account = IncubateeAccount.objects.get(user__email="journey.rep@gmail.com")
        activation_url = next(
            part for part in mail.outbox[-1].body.split() if "password-reset/" in part
        )
        activation = self.client.get(urlsplit(activation_url).path, follow=True)
        self.assertEqual(activation.status_code, 200)
        password = "Journey-Activation-Password-2026!"
        activated = self.client.post(
            activation.request["PATH_INFO"],
            {"new_password1": password, "new_password2": password},
        )
        self.assertEqual(activated.status_code, 302)
        account.refresh_from_db()
        self.assertEqual(account.invitation_status, "accepted")

        login = self.client.post(
            reverse("accounts:login"),
            {"username": account.user.email, "password": password},
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(
            self.client.get(
                reverse("incubation:business_profile", args=(self.business.pk,))
            ).status_code,
            200,
        )
        uploaded = self.upload_document(title="Journey business plan")
        self.assertEqual(uploaded.status_code, 302)
        document = BusinessDocument.objects.get(title="Journey business plan")

        progress = self.client.post(
            reverse("incubation:progress_submit", args=(self.business.pk,)),
            self.progress_data(),
        )
        self.assertEqual(progress.status_code, 302)
        submission = IncubateeProgressSubmission.objects.get()
        milestone = self.client.post(
            reverse("incubation:milestone_submit", args=(self.business.pk,)),
            {
                "title": "Journey first customer",
                "milestone_type": IncubateeMilestone.Type.FIRST_SALE,
                "description": "First customer order completed.",
                "achieved_on": date.today().isoformat(),
            },
        )
        self.assertEqual(milestone.status_code, 302)
        support = self.client.post(
            reverse("incubation:support_request_submit", args=(self.business.pk,)),
            {
                "category": IncubateeSupportRequest.Category.MARKETING,
                "description": "Please review our marketing plan.",
            },
        )
        self.assertEqual(support.status_code, 302)
        support_request = IncubateeSupportRequest.objects.get()

        self.login(self.staff)
        document_review = self.client.post(
            reverse(
                "incubation:business_document_review",
                args=(self.business.pk, document.pk),
            ),
            {"decision": "approve", "review_note": "Journey document reviewed."},
        )
        progress_review = self.client.post(
            reverse("incubation:progress_review", args=("progress", submission.pk)),
            {
                "status": IncubateeProgressReview.Status.REVIEWED,
                "review_note": "Journey progress reviewed.",
            },
        )
        support_review = self.client.post(
            reverse("incubation:support_request_review", args=(support_request.pk,)),
            {
                "status": IncubateeSupportRequest.Status.UNDER_REVIEW,
                "assigned_to": str(self.staff.pk),
                "response": "The request is under review.",
            },
        )
        self.assertEqual(document_review.status_code, 302)
        self.assertEqual(progress_review.status_code, 302)
        self.assertEqual(support_review.status_code, 302)

        document.refresh_from_db()
        support_request.refresh_from_db()
        self.assertEqual(document.status, BusinessDocument.Status.APPROVED)
        self.assertEqual(
            support_request.status,
            IncubateeSupportRequest.Status.UNDER_REVIEW,
        )
        account.user.refresh_from_db()
        self.login(account.user)
        report = self.client.get(
            reverse("incubation:incubatee_reports", args=(self.business.pk,))
        )
        foreign_report = self.client.get(
            reverse("incubation:incubatee_reports", args=(self.other_business.pk,))
        )
        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "12500.50")
        self.assertContains(report, "Journey first customer")
        self.assertEqual(foreign_report.status_code, 404)
