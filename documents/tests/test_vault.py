from io import BytesIO
import tempfile
from datetime import timedelta
from pathlib import Path
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.access import share_record
from accounts.models import Role, RoleGrant, TOTPDevice
from audit.models import AuditEvent
from documents.models import Document, DocumentVersion, ExpiryAlert
from documents.tasks import send_expiry_alerts
from documents.validators import MAX_DOCUMENT_SIZE, validate_document_file

User = get_user_model()


class DocumentVaultTests(TestCase):
    def setUp(self):
        self.storage_dir = tempfile.TemporaryDirectory()
        self.storage_override = override_settings(PRIVATE_MEDIA_ROOT=Path(self.storage_dir.name))
        self.storage_override.enable()
        self.addCleanup(self.storage_override.disable)
        self.addCleanup(self.storage_dir.cleanup)

        self.admin = User.objects.create_user(
            email="vault-admin@gmail.com",
            password="Only-for-tests-9!",
        )
        self.admin.roles.add(Role.objects.get(code=Role.Code.ADMIN))
        TOTPDevice.objects.create(
            user=self.admin,
            secret_encrypted="test-only-encrypted-secret",
            confirmed=True,
        )
        self.client.force_login(self.admin)
        session = self.client.session
        session["mfa_verified_user"] = self.admin.pk
        session.save()

    @staticmethod
    def pdf_upload(name="centre-policy.pdf", content=b"%PDF-1.7\nsynthetic test document"):
        return SimpleUploadedFile(name, content, content_type="application/pdf")

    def upload_document(self, **overrides):
        data = {
            "title": "Synthetic centre policy",
            "category": Document.Category.POLICIES,
            "reference_number": "TEST-001",
            "issuing_authority": "Synthetic test authority",
            "issue_date": "2025-01-01",
            "expiry_date": "",
            "sensitivity": Document.Sensitivity.INTERNAL,
            "owner": self.admin.pk,
            "notes": "Test data only.",
            "tags": "synthetic, policy",
            "file": self.pdf_upload(),
            "change_note": "Initial upload",
        }
        data.update(overrides)
        return self.client.post(reverse("documents:create"), data)

    def test_upload_replacement_history_private_download_and_audit(self):
        response = self.upload_document()
        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(title="Synthetic centre policy")
        first = document.versions.get()
        self.assertEqual(first.version_number, 1)
        self.assertEqual(first.metadata_snapshot["title"], document.title)

        detail = self.client.get(reverse("documents:detail", args=(document.pk,)))
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(
            AuditEvent.objects.filter(
                document_uuid=document.pk,
                action=AuditEvent.Action.DOCUMENT_VIEW,
            ).exists()
        )
        with self.assertRaises(PermissionDenied):
            AuditEvent.objects.filter(document_uuid=document.pk).update(user_agent="tampered")

        replacement = self.client.post(
            reverse("documents:replace", args=(document.pk,)),
            {
                "title": "Synthetic replacement policy",
                "category": document.category,
                "reference_number": document.reference_number,
                "issuing_authority": document.issuing_authority,
                "issue_date": "2025-01-01",
                "expiry_date": "",
                "sensitivity": document.sensitivity,
                "owner": self.admin.pk,
                "notes": document.notes,
                "tags": "synthetic, policy",
                "change_note": "Updated test version",
                "file": self.pdf_upload("replacement.pdf", b"%PDF-1.7\nreplacement"),
            },
        )
        self.assertEqual(replacement.status_code, 302)
        versions = list(document.versions.order_by("version_number"))
        self.assertEqual([item.version_number for item in versions], [1, 2])
        self.assertEqual([item.is_current for item in versions], [False, True])
        self.assertEqual(versions[0].metadata_snapshot["title"], "Synthetic centre policy")
        self.assertEqual(versions[1].metadata_snapshot["title"], "Synthetic replacement policy")
        storage = versions[0].file.storage
        self.assertTrue(storage.exists(versions[0].file.name))
        history = self.client.get(reverse("documents:versions", args=(document.pk,)))
        self.assertContains(history, "Initial upload")
        self.assertContains(history, "Updated test version")

        download = self.client.get(
            reverse("documents:version_download", args=(document.pk, versions[0].pk))
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.7\nsynthetic test document")
        self.assertTrue(
            AuditEvent.objects.filter(
                document_uuid=document.pk,
                version_uuid=versions[0].pk,
                action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
            ).exists()
        )
        with self.assertRaises(ValueError):
            versions[0].file.storage.url(versions[0].file.name)

    def test_upload_validates_file_type_and_size(self):
        invalid = SimpleUploadedFile("not-a-pdf.pdf", b"plain text")
        with self.assertRaisesMessage(ValidationError, "not a valid PDF"):
            validate_document_file(invalid)
        oversized = SimpleUploadedFile("large.pdf", b"x" * (MAX_DOCUMENT_SIZE + 1))
        with self.assertRaisesMessage(ValidationError, "must not exceed 25 MB"):
            validate_document_file(oversized)

        response = self.upload_document(file=SimpleUploadedFile("bad.pdf", b"not pdf"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Document.objects.exists())

    def test_docx_xlsx_and_raster_images_are_accepted(self):
        packages = (
            (
                "sample.docx",
                {"[Content_Types].xml": "<Types/>", "word/document.xml": "<document/>"},
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                "sample.xlsx",
                {"[Content_Types].xml": "<Types/>", "xl/workbook.xml": "<workbook/>"},
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        )
        for filename, members, expected_type in packages:
            with self.subTest(filename=filename):
                package = BytesIO()
                with ZipFile(package, "w") as archive:
                    for name, content in members.items():
                        archive.writestr(name, content)
                upload = SimpleUploadedFile(filename, package.getvalue())
                self.assertEqual(validate_document_file(upload), expected_type)

        image = BytesIO()
        Image.new("RGB", (2, 2), color="white").save(image, format="PNG")
        self.assertEqual(
            validate_document_file(SimpleUploadedFile("sample.png", image.getvalue())),
            "image/png",
        )

    def test_predefined_categories_match_the_srs(self):
        self.assertEqual(
            [label for _value, label in Document.Category.choices],
            [
                "Incorporation (CIPC)",
                "Policies",
                "QMS",
                "COIDA",
                "SARS and tax PINs",
                "B-BBEE",
                "Banking",
                "Governance and board minutes",
                "Contracts",
                "Other",
            ],
        )

    def test_role_and_sensitivity_restrict_confidential_and_restricted_documents(self):
        self.upload_document(
            title="Confidential synthetic contract",
            sensitivity=Document.Sensitivity.CONFIDENTIAL,
        )
        confidential = Document.objects.get(title="Confidential synthetic contract")
        system_admin = User.objects.create_user(
            email="system-admin@gmail.com",
            password="Only-for-tests-9!",
        )
        system_admin.roles.add(Role.objects.get(code=Role.Code.SYSTEM_ADMIN))
        TOTPDevice.objects.create(
            user=system_admin,
            secret_encrypted="test-only-encrypted-secret",
            confirmed=True,
        )
        self.client.force_login(system_admin)
        session = self.client.session
        session["mfa_verified_user"] = system_admin.pk
        session.save()
        response = self.client.get(reverse("documents:detail", args=(confidential.pk,)))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Access denied", status_code=403)

        super_admin = User.objects.create_user(
            email="super-admin@gmail.com",
            password="Only-for-tests-9!",
        )
        super_admin.roles.add(Role.objects.get(code=Role.Code.SUPER_ADMIN))
        TOTPDevice.objects.create(
            user=super_admin,
            secret_encrypted="test-only-encrypted-secret",
            confirmed=True,
        )
        self.client.force_login(super_admin)
        session = self.client.session
        session["mfa_verified_user"] = super_admin.pk
        session.save()
        self.upload_document(
            title="Restricted synthetic record",
            sensitivity=Document.Sensitivity.RESTRICTED,
        )
        restricted = Document.objects.get(title="Restricted synthetic record")
        self.client.force_login(self.admin)
        session = self.client.session
        session["mfa_verified_user"] = self.admin.pk
        session.save()
        self.assertEqual(
            self.client.get(reverse("documents:detail", args=(restricted.pk,))).status_code,
            403,
        )

    def test_archive_preserves_document_and_versions(self):
        self.upload_document()
        document = Document.objects.get(title="Synthetic centre policy")
        response = self.client.post(reverse("documents:archive", args=(document.pk,)))
        self.assertRedirects(response, reverse("documents:detail", args=(document.pk,)))
        document.refresh_from_db()
        self.assertTrue(document.archived)
        self.assertEqual(document.versions.count(), 1)
        self.assertFalse(Document.objects.filter(archived=False).exists())

    def test_search_includes_metadata_and_tags(self):
        self.upload_document()
        for query in ("TEST-001", "Synthetic test authority", "synthetic"):
            with self.subTest(query=query):
                response = self.client.get(reverse("documents:list"), {"q": query})
                self.assertContains(response, "Synthetic centre policy")

    def test_record_share_cannot_override_system_admin_confidential_restriction(self):
        self.upload_document(
            title="Confidential synthetic contract",
            sensitivity=Document.Sensitivity.CONFIDENTIAL,
        )
        document = Document.objects.get(title="Confidential synthetic contract")
        system_admin = User.objects.create_user(
            email="shared-system-admin@gmail.com",
            password="Only-for-tests-9!",
        )
        system_admin.roles.add(Role.objects.get(code=Role.Code.SYSTEM_ADMIN))
        with self.assertRaises(PermissionDenied):
            share_record(
                self.admin,
                system_admin,
                document,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                [RoleGrant.Action.READ],
            )

    def test_compliance_board_reports_expiry_states_without_inventing_missing_categories(self):
        today = timezone.localdate()
        documents = (
            ("Valid synthetic record", today + timedelta(days=61)),
            ("Expiring synthetic record", today + timedelta(days=60)),
            ("Expired synthetic record", today - timedelta(days=1)),
            ("Undated synthetic record", None),
        )
        for title, expiry_date in documents:
            Document.objects.create(
                title=title,
                category=Document.Category.POLICIES,
                expiry_date=expiry_date,
                sensitivity=Document.Sensitivity.INTERNAL,
                owner=self.admin,
                created_by=self.admin,
            )
        response = self.client.get(reverse("documents:compliance"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["status_counts"],
            {"valid": 1, "expiring": 1, "expired": 1, "no_expiry": 1},
        )
        self.assertContains(response, "Missing-document rules are not configured")

    def test_anonymous_user_cannot_access_document_list(self):
        self.client.logout()
        response = self.client.get(reverse("documents:list"))
        self.assertRedirects(response, f"{reverse('accounts:login')}?next={reverse('documents:list')}")


class DocumentExpiryAlertTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="expiry-admin@gmail.com",
            password="Only-for-tests-9!",
        )
        self.user.roles.add(Role.objects.get(code=Role.Code.ADMIN))

    def create_expiring_document(self, expiry_date):
        document = Document.objects.create(
            title="Synthetic expiring certificate",
            category=Document.Category.INCORPORATION,
            expiry_date=expiry_date,
            sensitivity=Document.Sensitivity.INTERNAL,
            owner=self.user,
            created_by=self.user,
        )
        return document

    @override_settings(DOCUMENT_EXPIRY_ALERT_RECIPIENTS=["alerts@gmail.com"])
    def test_creates_and_emails_60_30_and_7_day_alerts(self):
        today = timezone.localdate()
        self.create_expiring_document(today + timedelta(days=7))
        result = send_expiry_alerts()
        self.assertEqual(result, {"created": 3, "emailed": 3, "failed": 0})
        self.assertEqual(ExpiryAlert.objects.count(), 3)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(
            set(ExpiryAlert.objects.values_list("days_before", flat=True)),
            {7, 30, 60},
        )
        self.assertEqual(send_expiry_alerts()["created"], 0)
        self.assertEqual(len(mail.outbox), 3)

    @override_settings(DOCUMENT_EXPIRY_ALERT_RECIPIENTS=[])
    def test_dashboard_alert_is_created_and_email_configuration_failure_is_visible(self):
        today = timezone.localdate()
        self.create_expiring_document(today + timedelta(days=60))
        result = send_expiry_alerts()
        self.assertEqual(result, {"created": 1, "emailed": 0, "failed": 1})
        alert = ExpiryAlert.objects.get()
        self.assertIn("No DOCUMENT_EXPIRY_ALERT_RECIPIENTS configured", alert.email_error)
        self.assertIsNone(alert.email_sent_at)
