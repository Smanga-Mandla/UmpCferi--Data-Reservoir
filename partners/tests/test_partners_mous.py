from io import BytesIO
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, TOTPDevice
from audit.models import AuditEvent
from partners.models import (
    MOU,
    MOUAttachment,
    MOUExpiryAlert,
    MOUHistory,
    MOUObligation,
    Partner,
    PartnerImage,
    PartnerRepresentative,
)
from partners.tasks import send_mou_expiry_alerts
from stakeholders.models import Stakeholder

User = get_user_model()


class PartnerMOUTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.storage_settings = override_settings(PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name))
        self.storage_settings.enable()
        self.addCleanup(self.storage_settings.disable)
        self.addCleanup(self.temp_dir.cleanup)

        self.admin = self.create_role_user("admin@gmail.com", Role.Code.ADMIN)
        self.client.force_login(self.admin)
        session = self.client.session
        session["mfa_verified_user"] = self.admin.pk
        session.save()

    @staticmethod
    def create_role_user(email, role_code):
        user = User.objects.create_user(email=email, password="Only-for-tests-9!")
        user.roles.add(Role.objects.get(code=role_code))
        TOTPDevice.objects.create(
            user=user,
            secret_encrypted="test-only-encrypted-secret",
            confirmed=True,
        )
        return user

    def create_stakeholder(self, email="contact@gmail.com", **kwargs):
        return Stakeholder.objects.create(
            name=kwargs.pop("name", "Synthetic Partner Contact"),
            title=kwargs.pop("title", "Partnership Lead"),
            email=email,
            phone=kwargs.pop("phone", "+27000000000"),
            owner=self.admin,
            created_by=self.admin,
            **kwargs,
        )

    def create_partner(self, **kwargs):
        return Partner.objects.create(
            name=kwargs.pop("name", "Synthetic Partner Organisation"),
            organisation_type=kwargs.pop("organisation_type", Partner.OrganisationType.NGO),
            owner=kwargs.pop("owner", self.admin),
            created_by=kwargs.pop("created_by", self.admin),
            **kwargs,
        )

    def create_mou(self, partner=None, **kwargs):
        return MOU.objects.create(
            partner=partner or self.create_partner(),
            title=kwargs.pop("title", "Synthetic Partnership Agreement"),
            status=kwargs.pop("status", MOU.Status.ACTIVE),
            owner=kwargs.pop("owner", self.admin),
            created_by=kwargs.pop("created_by", self.admin),
            **kwargs,
        )

    @staticmethod
    def make_image(name="partner.png", image_format="PNG", color=(25, 90, 160)):
        image_bytes = BytesIO()
        Image.new("RGB", (16, 16), color).save(image_bytes, format=image_format)
        content_type = {
            "GIF": "image/gif",
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "WEBP": "image/webp",
        }[image_format]
        return SimpleUploadedFile(name, image_bytes.getvalue(), content_type=content_type)

    def test_partner_image_upload_is_private_authorized_and_keeps_history(self):
        create_response = self.client.post(
            reverse("partners:partner_create"),
            {
                "name": "Synthetic image partner",
                "registration_number": "",
                "organisation_type": Partner.OrganisationType.NGO,
                "sector": "",
                "address": "",
                "website": "",
                "province": "",
                "bbbee_level": "",
                "sensitivity": "internal",
                "image": self.make_image("partner-mark.png"),
            },
        )
        self.assertEqual(create_response.status_code, 302)
        partner = Partner.objects.get(name="Synthetic image partner")
        first_image = partner.current_image
        self.assertIsNotNone(first_image)
        self.assertTrue(first_image.file.name.startswith(f"partners/{partner.pk}/images/"))
        with self.assertRaises(ValueError):
            first_image.file.url

        detail = self.client.get(reverse("partners:partner_detail", args=(partner.pk,)))
        self.assertContains(
            detail,
            reverse("partners:partner_image", args=(partner.pk, first_image.pk)),
        )
        image_response = self.client.get(
            reverse("partners:partner_image", args=(partner.pk, first_image.pk))
        )
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response["Content-Type"], "image/png")
        self.assertEqual(image_response["Cache-Control"], "private, no-store")
        self.assertEqual(image_response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            AuditEvent.objects.filter(
                action=AuditEvent.Action.DOCUMENT_VIEW,
                resource="partners.partner_image",
            ).count(),
            1,
        )

        update_response = self.client.post(
            reverse("partners:partner_update", args=(partner.pk,)),
            {
                "name": partner.name,
                "registration_number": "",
                "organisation_type": Partner.OrganisationType.NGO,
                "sector": "",
                "address": "",
                "website": "",
                "province": "",
                "bbbee_level": "",
                "sensitivity": "internal",
                "image": self.make_image("partner-mark.webp", "WEBP", color=(30, 140, 80)),
            },
        )
        self.assertEqual(update_response.status_code, 302)
        images = list(partner.images.order_by("-uploaded_at", "-pk"))
        self.assertEqual(len(images), 2)
        self.assertNotEqual(images[0].pk, first_image.pk)
        self.assertEqual(partner.current_image.pk, images[0].pk)
        self.assertTrue(first_image.file.storage.exists(first_image.file.name))

        guest = User.objects.create_user(email="image-viewer@gmail.com")
        guest.access_expires_at = timezone.now() + timedelta(days=10)
        guest.save()
        guest.roles.add(Role.objects.get(code=Role.Code.EXTERNAL_GUEST))
        self.client.force_login(guest)
        self.assertEqual(
            self.client.get(
                reverse("partners:partner_image", args=(partner.pk, first_image.pk))
            ).status_code,
            403,
        )

    def test_partner_image_upload_rejects_non_image_file(self):
        response = self.client.post(
            reverse("partners:partner_create"),
            {
                "name": "Synthetic invalid image partner",
                "registration_number": "",
                "organisation_type": Partner.OrganisationType.NGO,
                "sector": "",
                "address": "",
                "website": "",
                "province": "",
                "bbbee_level": "",
                "sensitivity": "internal",
                "image": self.make_image("unsupported.gif", "GIF"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload a valid JPEG, PNG, or WebP image.")
        self.assertFalse(Partner.objects.filter(name="Synthetic invalid image partner").exists())

    def test_partner_crud_and_soft_deletion(self):
        response = self.client.post(
            reverse("partners:partner_create"),
            {
                "name": "Synthetic registered organisation",
                "registration_number": "TEST-REG-01",
                "organisation_type": Partner.OrganisationType.ACADEMIC,
                "sector": "Research",
                "address": "Test address only",
                "website": "https://example.test",
                "province": "Mpumalanga",
                "bbbee_level": "Level 2",
                "sensitivity": "internal",
            },
        )
        self.assertEqual(response.status_code, 302)
        partner = Partner.objects.get(name="Synthetic registered organisation")
        self.assertEqual(partner.registration_number, "TEST-REG-01")
        updated = self.client.post(
            reverse("partners:partner_update", args=(partner.pk,)),
            {
                "name": "Updated synthetic organisation",
                "registration_number": partner.registration_number,
                "organisation_type": Partner.OrganisationType.PRIVATE,
                "sector": "Research",
                "address": "",
                "website": "",
                "province": "",
                "bbbee_level": "",
                "sensitivity": "internal",
            },
        )
        self.assertEqual(updated.status_code, 302)
        partner.refresh_from_db()
        self.assertEqual(partner.name, "Updated synthetic organisation")
        removed = self.client.post(reverse("partners:partner_delete", args=(partner.pk,)))
        self.assertEqual(removed.status_code, 302)
        self.assertFalse(Partner.objects.filter(pk=partner.pk).exists())
        self.assertTrue(Partner.all_objects.filter(pk=partner.pk, is_deleted=True).exists())

    def test_restricted_records_cannot_be_created_by_admin(self):
        partner_response = self.client.post(
            reverse("partners:partner_create"),
            {
                "name": "Restricted synthetic partner",
                "registration_number": "",
                "organisation_type": Partner.OrganisationType.NGO,
                "sector": "",
                "address": "",
                "website": "",
                "province": "",
                "bbbee_level": "",
                "sensitivity": "restricted",
            },
        )
        self.assertEqual(partner_response.status_code, 403)
        self.assertFalse(Partner.objects.filter(name="Restricted synthetic partner").exists())

        partner = self.create_partner()
        mou_response = self.client.post(
            reverse("partners:mou_create"),
            {
                "partner": partner.pk,
                "title": "Restricted synthetic agreement",
                "reference": "",
                "purpose": "",
                "signing_date": "",
                "start_date": "",
                "end_date": "",
                "renewal_terms": "",
                "value_zar": "",
                "status": MOU.Status.DRAFT,
                "cferi_signatory": "",
                "primary_representative": "",
                "secondary_representative": "",
                "renewed_from": "",
                "sensitivity": "restricted",
            },
        )
        self.assertEqual(mou_response.status_code, 403)
        self.assertFalse(MOU.objects.filter(title="Restricted synthetic agreement").exists())

    def test_representative_reuses_stakeholder_record_and_prevents_duplicate_people(self):
        partner = self.create_partner()
        contact = self.create_stakeholder()
        duplicate = self.client.post(
            reverse("stakeholders:create"),
            {
                "name": "Duplicate contact",
                "title": "Other title",
                "email": "CONTACT@gmail.com",
                "phone": "",
                "sensitivity": "internal",
            },
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(Stakeholder.objects.filter(email__iexact=contact.email).count(), 1)

        linked = self.client.post(
            reverse("partners:representative_add", args=(partner.pk,)),
            {"stakeholder": contact.pk},
        )
        self.assertEqual(linked.status_code, 302)
        self.assertEqual(Stakeholder.objects.count(), 1)
        self.assertTrue(PartnerRepresentative.objects.filter(partner=partner, stakeholder=contact).exists())
        detail = self.client.get(reverse("partners:partner_detail", args=(partner.pk,)))
        self.assertContains(detail, contact.name)
        self.assertContains(detail, contact.email)
        self.assertEqual(
            self.client.post(
                reverse("partners:representative_remove", args=(partner.pk, contact.pk))
            ).status_code,
            302,
        )
        self.assertFalse(
            PartnerRepresentative.objects.filter(
                partner=partner,
                stakeholder=contact,
                is_active=True,
            ).exists()
        )
        self.client.post(
            reverse("partners:representative_add", args=(partner.pk,)),
            {"stakeholder": contact.pk},
        )
        self.assertEqual(
            PartnerRepresentative.objects.filter(
                partner=partner,
                stakeholder=contact,
            ).count(),
            2,
        )

    def test_mou_creation_folder_attachment_download_and_history(self):
        partner = self.create_partner()
        primary = self.create_stakeholder()
        PartnerRepresentative.objects.create(
            partner=partner,
            stakeholder=primary,
            linked_by=self.admin,
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("partners:mou_create"),
                {
                    "partner": partner.pk,
                    "title": "Synthetic funding agreement",
                    "reference": "MOU-TEST-01",
                    "purpose": "Synthetic test purpose",
                    "signing_date": "2026-01-01",
                    "start_date": "2026-01-01",
                    "end_date": "2027-01-01",
                    "renewal_terms": "Review before end date",
                    "value_zar": "1000.00",
                    "status": MOU.Status.ACTIVE,
                    "cferi_signatory": "Test Signatory",
                    "primary_representative": primary.pk,
                    "secondary_representative": "",
                    "renewed_from": "",
                    "sensitivity": "internal",
                },
            )
        self.assertEqual(response.status_code, 302)
        mou = MOU.objects.get(title="Synthetic funding agreement")
        folder = Path(self.temp_dir.name) / "mous" / str(mou.pk)
        self.assertTrue(folder.is_dir())
        self.assertEqual(mou.history.count(), 1)
        self.assertEqual(mou.history.first().snapshot["reference"], "MOU-TEST-01")

        attachment = SimpleUploadedFile(
            "signed-agreement.pdf",
            b"synthetic agreement contents",
            content_type="application/pdf",
        )
        uploaded = self.client.post(
            reverse("partners:attachment_add", args=(mou.pk,)),
            {
                "category": MOUAttachment.Category.SIGNED_MOU,
                "title": "Signed agreement",
                "file": attachment,
            },
        )
        self.assertEqual(uploaded.status_code, 302)
        stored_attachment = mou.attachments.get()
        self.assertEqual(
            Path(self.temp_dir.name, stored_attachment.file.name).parent,
            folder / MOUAttachment.Category.SIGNED_MOU,
        )
        detail = self.client.get(reverse("partners:mou_detail", args=(mou.pk,)))
        self.assertContains(detail, primary.name)
        self.assertContains(detail, "Signed agreement")
        download = self.client.get(
            reverse(
                "partners:attachment_download",
                args=(mou.pk, stored_attachment.pk),
            )
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"synthetic agreement contents")
        self.assertEqual(download["Cache-Control"], "private, no-store")
        self.assertTrue(
            AuditEvent.objects.filter(
                resource="partners.mou_attachment",
                action=AuditEvent.Action.DOCUMENT_VIEW,
                document_uuid=mou.pk,
                version_uuid=stored_attachment.pk,
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                resource="partners.mou_attachment",
                action=AuditEvent.Action.DOCUMENT_DOWNLOAD,
                document_uuid=mou.pk,
                version_uuid=stored_attachment.pk,
            ).exists()
        )

        updated = self.client.post(
            reverse("partners:mou_update", args=(mou.pk,)),
            {
                "partner": partner.pk,
                "title": "Updated synthetic funding agreement",
                "reference": "MOU-TEST-01",
                "purpose": "Updated purpose",
                "signing_date": "2026-01-01",
                "start_date": "2026-01-01",
                "end_date": "2027-01-01",
                "renewal_terms": "Review annually",
                "value_zar": "1250.00",
                "status": MOU.Status.ACTIVE,
                "cferi_signatory": "Test Signatory",
                "primary_representative": primary.pk,
                "secondary_representative": "",
                "renewed_from": "",
                "sensitivity": "internal",
            },
        )
        self.assertEqual(updated.status_code, 302)
        self.assertEqual(mou.history.count(), 2)
        self.assertEqual(mou.history.first().snapshot["title"], "Updated synthetic funding agreement")
        with self.assertRaises(PermissionDenied):
            MOUHistory.objects.filter(mou=mou).update(snapshot={})

    def test_obligation_create_and_update(self):
        mou = self.create_mou()
        added = self.client.post(
            reverse("partners:obligation_add", args=(mou.pk,)),
            {
                "title": "Synthetic reporting deliverable",
                "description": "Test-only requirement",
                "owner": self.admin.pk,
                "due_date": "2026-12-01",
                "status": MOUObligation.Status.OPEN,
            },
        )
        self.assertEqual(added.status_code, 302)
        obligation = MOUObligation.objects.get(mou=mou)
        self.assertEqual(obligation.owner, self.admin)
        edited = self.client.post(
            reverse("partners:obligation_update", args=(mou.pk, obligation.pk)),
            {
                "title": obligation.title,
                "description": obligation.description,
                "owner": self.admin.pk,
                "due_date": "2026-12-01",
                "status": MOUObligation.Status.COMPLETED,
            },
        )
        self.assertEqual(edited.status_code, 302)
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, MOUObligation.Status.COMPLETED)

    def test_partner_permission_matrix_restricts_guest_and_system_admin_confidential_data(self):
        partner = self.create_partner()
        mou = self.create_mou(partner)
        attachment = MOUAttachment.objects.create(
            mou=mou,
            category=MOUAttachment.Category.ANNEXURE,
            title="Synthetic restricted annexure",
            file=SimpleUploadedFile("annexure.pdf", b"private test attachment"),
            original_filename="annexure.pdf",
            uploaded_by=self.admin,
        )
        system_admin = self.create_role_user("system@gmail.com", Role.Code.SYSTEM_ADMIN)
        self.client.force_login(system_admin)
        session = self.client.session
        session["mfa_verified_user"] = system_admin.pk
        session.save()
        self.assertEqual(
            self.client.get(reverse("partners:partner_detail", args=(partner.pk,))).status_code,
            200,
        )
        confidential_mou = self.create_mou(
            partner,
            title="Confidential synthetic MOU",
            sensitivity="confidential",
        )
        self.assertEqual(
            self.client.get(reverse("partners:mou_detail", args=(confidential_mou.pk,))).status_code,
            403,
        )

        guest = User.objects.create_user(email="guest@gmail.com", password="Only-for-tests-9!")
        guest.access_expires_at = timezone.now() + timedelta(days=10)
        guest.save()
        guest.roles.add(Role.objects.get(code=Role.Code.EXTERNAL_GUEST))
        self.client.force_login(guest)
        self.assertEqual(
            self.client.get(reverse("partners:partner_detail", args=(partner.pk,))).status_code,
            403,
        )
        guest_owned_mou = self.create_mou(
            partner,
            title="Synthetic guest-owned MOU",
            owner=guest,
        )
        self.assertEqual(
            self.client.get(reverse("partners:mou_detail", args=(guest_owned_mou.pk,))).status_code,
            403,
        )
        guest_partner = self.create_partner(
            name="Synthetic guest-owned partner",
            owner=guest,
        )
        accessible_mou = self.create_mou(
            guest_partner,
            title="Synthetic accessible guest MOU",
            owner=guest,
        )
        MOUObligation.objects.create(
            mou=accessible_mou,
            title="Unassigned confidential obligation",
            owner=self.admin,
            created_by=self.admin,
        )
        mou_page = self.client.get(reverse("partners:mou_detail", args=(accessible_mou.pk,)))
        self.assertEqual(mou_page.status_code, 200)
        self.assertNotContains(mou_page, "Unassigned confidential obligation")
        self.assertEqual(
            self.client.get(
                reverse(
                    "partners:attachment_download",
                    args=(mou.pk, attachment.pk),
                )
            ).status_code,
            403,
        )

    def test_partner_and_mou_delete_permissions_are_enforced(self):
        partner = self.create_partner()
        mou = self.create_mou(partner)
        viewer = self.create_role_user("board@gmail.com", Role.Code.BOARD_MEMBER)
        self.client.force_login(viewer)
        session = self.client.session
        session["mfa_verified_user"] = viewer.pk
        session.save()
        self.assertEqual(self.client.get(reverse("partners:partner_detail", args=(partner.pk,))).status_code, 200)
        self.assertEqual(self.client.post(reverse("partners:partner_delete", args=(partner.pk,))).status_code, 403)
        self.assertEqual(self.client.post(reverse("partners:mou_delete", args=(mou.pk,))).status_code, 403)

    def test_representative_link_history_is_retained_and_active_mou_links_are_protected(self):
        partner = self.create_partner()
        contact = self.create_stakeholder()
        link = PartnerRepresentative.objects.create(
            partner=partner,
            stakeholder=contact,
            linked_by=self.admin,
        )
        mou = self.create_mou(partner, primary_representative=contact)
        self.client.post(
            reverse("partners:representative_remove", args=(partner.pk, contact.pk))
        )
        link.refresh_from_db()
        self.assertTrue(link.is_active)
        self.assertIsNone(link.unlinked_at)
        self.assertEqual(mou.history.count(), 0)
        self.assertEqual(
            self.client.post(
                reverse("stakeholders:delete", args=(contact.pk,))
            ).status_code,
            403,
        )


class MOUExpiryAlertTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="mou-alert@gmail.com", password="Only-for-tests-9!")
        self.user.roles.add(Role.objects.get(code=Role.Code.ADMIN))
        TOTPDevice.objects.create(
            user=self.user,
            secret_encrypted="test-only-encrypted-secret",
            confirmed=True,
        )

    def create_active_mou(self, end_date):
        partner = Partner.objects.create(
            name=f"Synthetic alert partner {end_date}",
            organisation_type=Partner.OrganisationType.FUNDER,
            owner=self.user,
            created_by=self.user,
        )
        return MOU.objects.create(
            partner=partner,
            title=f"Synthetic alert MOU {end_date}",
            end_date=end_date,
            status=MOU.Status.ACTIVE,
            owner=self.user,
            created_by=self.user,
        )

    @override_settings(MOU_ALERT_RECIPIENTS=["mou-alerts@gmail.com"])
    def test_generates_dashboard_and_email_alerts_at_90_60_and_30_days(self):
        today = timezone.localdate()
        self.create_active_mou(today + timedelta(days=30))
        result = send_mou_expiry_alerts()
        self.assertEqual(result, {"created": 3, "emailed": 3, "failed": 0})
        self.assertEqual(set(MOUExpiryAlert.objects.values_list("days_before", flat=True)), {30, 60, 90})
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(send_mou_expiry_alerts()["created"], 0)
        self.assertEqual(len(mail.outbox), 3)
        client = self.client
        client.force_login(self.user)
        session = client.session
        session["mfa_verified_user"] = self.user.pk
        session.save()
        dashboard = client.get(reverse("partners:mou_expiry_alerts"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "30-day reminder")

    @override_settings(MOU_ALERT_RECIPIENTS=[])
    def test_missing_email_configuration_is_visible_on_dashboard_alert(self):
        self.create_active_mou(timezone.localdate() + timedelta(days=90))
        result = send_mou_expiry_alerts()
        self.assertEqual(result, {"created": 1, "emailed": 0, "failed": 1})
        alert = MOUExpiryAlert.objects.get()
        self.assertIn("No MOU_ALERT_RECIPIENTS configured", alert.email_error)

    @override_settings(MOU_ALERT_RECIPIENTS=["mou-alerts@gmail.com"])
    def test_sensitive_agreement_email_is_suppressed_without_sensitive_recipient_policy(self):
        mou = self.create_active_mou(
            timezone.localdate() + timedelta(days=30),
        )
        mou.sensitivity = "confidential"
        mou.save(update_fields=("sensitivity",))
        result = send_mou_expiry_alerts()
        self.assertEqual(result["emailed"], 0)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(MOUExpiryAlert.objects.count(), 3)
        self.assertTrue(
            all(
                "sensitivity-aware recipient policy" in error
                for error in MOUExpiryAlert.objects.values_list("email_error", flat=True)
            )
        )
