from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse
from openpyxl import Workbook

from accounts.models import Role, TOTPDevice
from incubation.models import (
    Cohort,
    FounderProfile,
    Incubatee,
    IncubateeAccessEvent,
    IncubateeConsent,
    IncubateeDocument,
    IncubateeFounder,
    IncubateeStatusHistory,
    Programme,
)
from incubation.security import decrypt_id_number
from stakeholders.models import Stakeholder, StakeholderTypeAssignment

User = get_user_model()


class IncubateeManagementTests(TestCase):
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
        self.admin = self.create_user("incubatee-admin@gmail.com", Role.Code.ADMIN)
        self.client.force_login(self.admin)
        self.verify_mfa(self.admin)

    def create_user(self, email, role_code):
        user = User.objects.create_user(
            email=email,
            password="synthetic-test-password",
            access_expires_at=(
                timezone.now() + timedelta(days=2)
                if role_code == Role.Code.EXTERNAL_GUEST
                else None
            ),
        )
        user.roles.add(Role.objects.get(code=role_code))
        if role_code in {
            Role.Code.SUPER_ADMIN,
            Role.Code.SYSTEM_ADMIN,
            Role.Code.ADMIN,
            Role.Code.BOARD_MEMBER,
        }:
            TOTPDevice.objects.create(
                user=user,
                secret_encrypted="synthetic-encrypted-test-secret",
                confirmed=True,
            )
        return user

    def verify_mfa(self, user):
        session = self.client.session
        session["mfa_verified_user"] = user.pk
        session.save()

    def create_incubatee(self, **kwargs):
        return Incubatee.objects.create(
            legal_name=kwargs.pop("legal_name", "Synthetic Test Company"),
            created_by=kwargs.pop("created_by", self.admin),
            updated_by=kwargs.pop("updated_by", self.admin),
            owner=kwargs.pop("owner", self.admin),
            **kwargs,
        )

    def create_founder(self, incubatee, *, name="Synthetic Founder", id_number="SYNTHETIC-ID-001"):
        stakeholder = Stakeholder.objects.create(
            name=name,
            title="Founder",
            owner=self.admin,
            created_by=self.admin,
        )
        StakeholderTypeAssignment.objects.create(
            stakeholder=stakeholder,
            relationship_type=Stakeholder.RelationshipType.FOUNDER,
            assigned_by=self.admin,
        )
        founder = FounderProfile.objects.create(
            stakeholder=stakeholder,
            id_number_encrypted="",
        )
        link = IncubateeFounder.objects.create(
            incubatee=incubatee,
            founder=founder,
            ownership_percentage=Decimal("60.00"),
        )
        self.client.post(
            reverse("incubation:onboarding_step", args=(incubatee.pk, 3)),
            {
                "founders-TOTAL_FORMS": "10",
                "founders-INITIAL_FORMS": "1",
                "founders-MIN_NUM_FORMS": "0",
                "founders-MAX_NUM_FORMS": "10",
                "founders-0-founder_link_id": str(link.pk),
                "founders-0-founder_name": name,
                "founders-0-id_number": id_number,
                "founders-0-gender": "woman",
                "founders-0-date_of_birth": "1990-01-01",
                "founders-0-race": "african",
                "founders-0-disability_status": "no",
                "founders-0-highest_qualification": "Synthetic qualification",
                "founders-0-prior_business_experience": "",
                "founders-0-ownership_percentage": "60",
            },
        )
        founder.refresh_from_db()
        return founder

    def test_onboarding_wizard_persists_company_business_and_financial_baseline(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("incubation:create"),
                {
                    "legal_name": "Synthetic Enterprise (Pty) Ltd",
                    "trading_name": "Synthetic Enterprise",
                    "cipc_registration_number": "SYN-CIPC-001",
                },
            )
        self.assertEqual(response.status_code, 302)
        incubatee = Incubatee.objects.get(cipc_registration_number="SYN-CIPC-001")
        self.assertTrue((Path(self.temp_dir.name) / "incubatees" / str(incubatee.pk)).is_dir())

        business = self.client.post(
            reverse("incubation:onboarding_step", args=(incubatee.pk, 2)),
            {
                "sic_code": "12345",
                "sub_sector": "Synthetic agritech",
                "products_services": "Synthetic service",
                "business_model": "b2b",
                "business_stage": "mvp",
                "formality": "formal",
            },
        )
        self.assertEqual(business.status_code, 302)
        incubatee.refresh_from_db()
        self.assertEqual(incubatee.business_model, Incubatee.BusinessModel.B2B)
        self.assertEqual(incubatee.sic_code, "12345")

        team = self.client.post(
            reverse("incubation:onboarding_step", args=(incubatee.pk, 4)),
            {
                "full_time_employees": "3",
                "part_time_employees": "0",
                "full_time_men": "1",
                "full_time_women": "2",
                "full_time_youth": "1",
                "part_time_men": "",
                "part_time_women": "",
                "part_time_youth": "",
            },
        )
        self.assertEqual(team.status_code, 302)
        incubatee.refresh_from_db()
        self.assertEqual(incubatee.full_time_employees, 3)
        self.assertIsNone(incubatee.part_time_men)

        financial = self.client.post(
            reverse("incubation:onboarding_step", args=(incubatee.pk, 5)),
            {
                "revenue_12_months_zar": "0.00",
                "monthly_average_revenue_zar": "",
                "funding_received_zar": "1250.75",
                "funding_sources": "Synthetic grant",
                "debt_zar": "",
                "has_bank_account": "false",
            },
        )
        self.assertEqual(financial.status_code, 302)
        incubatee.refresh_from_db()
        self.assertEqual(incubatee.revenue_12_months_zar, Decimal("0.00"))
        self.assertIsNone(incubatee.monthly_average_revenue_zar)
        self.assertEqual(incubatee.funding_received_zar, Decimal("1250.75"))
        self.assertEqual(incubatee.baseline_history.count(), 4)

    def test_founder_data_is_reused_encrypted_and_personal_data_access_is_restricted(self):
        incubatee = self.create_incubatee()
        post_data = {
            "founders-TOTAL_FORMS": "10",
            "founders-INITIAL_FORMS": "0",
            "founders-MIN_NUM_FORMS": "0",
            "founders-MAX_NUM_FORMS": "10",
            "founders-0-founder_name": "Synthetic Founder One",
            "founders-0-id_number": "SYNTHETIC-IDENTIFIER-01",
            "founders-0-gender": "woman",
            "founders-0-date_of_birth": "1990-01-01",
            "founders-0-race": "african",
            "founders-0-disability_status": "no",
            "founders-0-highest_qualification": "Synthetic diploma",
            "founders-0-prior_business_experience": "Synthetic history",
            "founders-0-ownership_percentage": "60.00",
        }
        response = self.client.post(
            reverse("incubation:onboarding_step", args=(incubatee.pk, 3)),
            post_data,
        )
        self.assertEqual(response.status_code, 302)
        link = IncubateeFounder.objects.get(incubatee=incubatee, is_active=True)
        profile = link.founder
        self.assertNotEqual(profile.id_number_encrypted, "SYNTHETIC-IDENTIFIER-01")
        self.assertEqual(decrypt_id_number(profile.id_number_encrypted), "SYNTHETIC-IDENTIFIER-01")
        self.assertEqual(profile.stakeholder.email, None)
        self.assertTrue(
            StakeholderTypeAssignment.objects.filter(
                stakeholder=profile.stakeholder,
                relationship_type=Stakeholder.RelationshipType.FOUNDER,
                is_active=True,
            ).exists()
        )
        detail = self.client.get(reverse("incubation:detail", args=(incubatee.pk,)))
        self.assertContains(detail, "Synthetic Founder One")
        self.assertContains(detail, profile.gender and "Woman")
        self.assertTrue(
            IncubateeAccessEvent.objects.filter(
                incubatee=incubatee,
                action=IncubateeAccessEvent.Action.PERSONAL_DATA_VIEW,
            ).exists()
        )

        staff = self.create_user("assigned-staff@gmail.com", Role.Code.CFERI_STAFF)
        incubatee.assigned_users.add(staff)
        self.client.force_login(staff)
        detail = self.client.get(reverse("incubation:detail", args=(incubatee.pk,)))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, "Synthetic Founder One")
        self.assertNotContains(detail, "SYNTHETIC-IDENTIFIER-01")
        self.assertEqual(
            self.client.get(
                reverse("incubation:onboarding_step", args=(incubatee.pk, 3))
            ).status_code,
            403,
        )

    def test_consent_purpose_history_and_exact_lifecycle_values(self):
        incubatee = self.create_incubatee()
        response = self.client.post(
            reverse("incubation:onboarding_step", args=(incubatee.pk, 8)),
            {
                "popia_status": "granted",
                "popia_date": "2026-10-20",
                "popia_method": "Synthetic signed form",
                "funder_status": "declined",
                "funder_date": "2026-10-20",
                "funder_method": "Synthetic verbal response",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(incubatee.consent_history.count(), 2)
        self.assertSetEqual(
            set(incubatee.consent_history.values_list("purpose", flat=True)),
            {
                IncubateeConsent.Purpose.POPIA,
                IncubateeConsent.Purpose.FUNDER_REPORTING,
            },
        )

        response = self.client.post(
            reverse("incubation:status", args=(incubatee.pk,)),
            {"status": Incubatee.Status.ACTIVE},
        )
        self.assertRedirects(
            response,
            reverse("incubation:detail", args=(incubatee.pk,)),
            fetch_redirect_response=False,
        )
        incubatee.refresh_from_db()
        self.assertEqual(incubatee.status, Incubatee.Status.ACTIVE)
        self.assertEqual(incubatee.status_history.count(), 1)
        self.assertEqual(
            set(Incubatee.Status.values),
            {"applicant", "onboarding", "active", "graduated", "exited", "withdrawn"},
        )

    def test_programme_cohort_staff_owner_and_mentor_assignments(self):
        incubatee = self.create_incubatee()
        programme = Programme.objects.create(name="Synthetic programme")
        cohort = Cohort.objects.create(programme=programme, name="Synthetic cohort")
        staff_owner = self.create_user("staff-owner@gmail.com", Role.Code.CFERI_STAFF)
        mentor = Stakeholder.objects.create(
            name="Synthetic mentor",
            title="Mentor",
            owner=self.admin,
            created_by=self.admin,
        )
        response = self.client.post(
            reverse("incubation:assignments", args=(incubatee.pk,)),
            {
                "programme": programme.pk,
                "cohort": cohort.pk,
                "staff_owner": staff_owner.pk,
                "mentors": [mentor.pk],
            },
        )
        self.assertEqual(response.status_code, 302)
        incubatee.refresh_from_db()
        self.assertEqual(incubatee.programme, programme)
        self.assertEqual(incubatee.cohort, cohort)
        self.assertEqual(incubatee.staff_owner, staff_owner)
        self.assertTrue(incubatee.assigned_users.filter(pk=staff_owner.pk).exists())
        self.assertQuerySetEqual(incubatee.mentors.all(), [mentor])

    def test_board_aggregate_list_excludes_restricted_records_and_suppresses_small_counts(self):
        self.client.logout()
        board = self.create_user("board-list@gmail.com", Role.Code.BOARD_MEMBER)
        self.client.force_login(board)
        self.verify_mfa(board)
        for index in range(4):
            self.create_incubatee(
                legal_name=f"Synthetic Restricted {index}",
                sensitivity=Incubatee.Sensitivity.RESTRICTED,
            )
        self.create_incubatee(
            legal_name="Synthetic Visible Board Record",
            sensitivity=Incubatee.Sensitivity.INTERNAL,
        )

        response = self.client.get(reverse("incubation:list"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["aggregate_only"])
        self.assertIsNone(response.context["total_count"])
        self.assertTrue(
            all(item["total"] is None for item in response.context["status_counts"])
        )
        self.assertContains(response, "Suppressed")

    def test_cipc_duplicates_score_validation_and_permissions(self):
        existing = self.create_incubatee(cipc_registration_number="SYN-DUP-001")
        duplicate = self.client.post(
            reverse("incubation:create"),
            {
                "legal_name": "Duplicate synthetic company",
                "cipc_registration_number": "syn-dup-001",
            },
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, "already registered")
        self.assertEqual(Incubatee.objects.filter(cipc_registration_number__iexact="syn-dup-001").count(), 1)

        invalid = self.client.post(
            reverse("incubation:onboarding_step", args=(existing.pk, 7)),
            {
                "finance_score": "6",
                "marketing_score": "",
                "operations_score": "",
                "compliance_score": "",
                "technology_score": "",
                "leadership_score": "",
                "support_need_1": "",
                "support_need_2": "",
                "support_need_3": "",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "less than or equal to 5")

        user_without_grant = self.create_user("no-incubatee-access@gmail.com", Role.Code.EXTERNAL_GUEST)
        self.client.force_login(user_without_grant)
        self.assertEqual(
            self.client.get(reverse("incubation:detail", args=(existing.pk,))).status_code,
            403,
        )

    def test_private_document_download_requires_admin_and_is_audited(self):
        incubatee = self.create_incubatee()
        uploaded = SimpleUploadedFile(
            "synthetic-business-plan.pdf",
            b"%PDF-1.4\nsynthetic private test data",
            content_type="application/pdf",
        )
        response = self.client.post(
            reverse("incubation:document_add", args=(incubatee.pk,)),
            {
                "category": IncubateeDocument.Category.BUSINESS_PLAN,
                "title": "Synthetic business plan",
                "file": uploaded,
            },
        )
        self.assertEqual(response.status_code, 302)
        document = IncubateeDocument.objects.get(incubatee=incubatee)
        self.assertTrue(
            (Path(self.temp_dir.name) / "incubatees" / str(incubatee.pk)).is_dir()
        )
        downloaded = self.client.get(
            reverse("incubation:document_download", args=(incubatee.pk, document.pk))
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded["Cache-Control"], "private, no-store")
        self.assertEqual(b"".join(downloaded.streaming_content), b"%PDF-1.4\nsynthetic private test data")
        self.assertTrue(
            IncubateeAccessEvent.objects.filter(
                incubatee=incubatee,
                action=IncubateeAccessEvent.Action.DOCUMENT_DOWNLOAD,
                object_id=str(document.pk),
            ).exists()
        )
        staff = self.create_user("doc-staff@gmail.com", Role.Code.CFERI_STAFF)
        incubatee.assigned_users.add(staff)
        self.client.force_login(staff)
        self.assertEqual(
            self.client.get(
                reverse("incubation:document_download", args=(incubatee.pk, document.pk))
            ).status_code,
            403,
        )

    def test_excel_import_preview_reports_duplicates_and_imports_valid_rows(self):
        self.create_incubatee(cipc_registration_number="SYN-EXISTING-01")
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["legal_name", "cipc_registration_number", "date_registered", "entity_type"])
        worksheet.append(["Synthetic imported company", "SYN-NEW-01", date(2026, 1, 2), "private_company"])
        worksheet.append(["Synthetic duplicate", "SYN-EXISTING-01", date(2026, 1, 3), "private_company"])
        data = BytesIO()
        workbook.save(data)
        data.seek(0)
        response = self.client.post(
            reverse("incubation:import"),
            {
                "workbook": SimpleUploadedFile(
                    "synthetic-import.xlsx",
                    data.getvalue(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synthetic imported company")
        self.assertContains(response, "already exists")
        self.assertEqual(Incubatee.objects.count(), 1)
        imported = self.client.post(
            reverse("incubation:import"),
            {"confirm": "yes"},
        )
        self.assertEqual(imported.status_code, 302)
        created = Incubatee.objects.get(cipc_registration_number="SYN-NEW-01")
        self.assertEqual(created.source, "excel_import")
        self.assertEqual(created.date_registered, date(2026, 1, 2))
        self.assertEqual(Incubatee.objects.count(), 2)
