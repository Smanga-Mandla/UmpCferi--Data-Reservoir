from io import StringIO

from django.apps import apps
from django.core.files.storage import storages
from django.core.management.base import CommandError
from django.core.management import call_command
from django.test import TestCase, override_settings

from accounts.models import User
from audit.models import AuditEvent
from documents.models import Document
from incubation.models import Incubatee, MonthlyCheckIn, Programme
from interactions.models import FollowUpTask, Interaction
from partners.models import MOU, Partner, PartnerImage


@override_settings(DEBUG=True)
class DemoDataSeedTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(
            email="demo.operator@gmail.com",
            password=None,
        )

    def tearDown(self):
        private_storage = storages["default"]
        for model, field_names in (
            ("partners.PartnerImage", ("file",)),
            ("documents.DocumentVersion", ("file",)),
            ("partners.MOUAttachment", ("file",)),
            ("incubation.IncubateeDocument", ("file",)),
            ("incubation.QuarterlyReview", ("management_accounts",)),
            ("incubation.MonthlyCheckInEvidence", ("file",)),
        ):
            app_label, model_name = model.split(".")
            model_class = apps.get_model(app_label, model_name)
            for record in model_class.objects.all():
                for field_name in field_names:
                    stored_file = getattr(record, field_name)
                    if stored_file and stored_file.name:
                        private_storage.delete(stored_file.name)

    def test_seed_adds_demo_records_and_is_idempotent(self):
        for _ in range(2):
            output = StringIO()
            call_command(
                "seed_demo_data",
                actor_email=self.actor.email,
                stdout=output,
            )
            self.assertIn("Synthetic demo data is ready", output.getvalue())

        self.assertEqual(Programme.objects.filter(name__startswith="DEMO ONLY -").count(), 1)
        self.assertEqual(Incubatee.objects.filter(legal_name__startswith="DEMO ONLY -").count(), 1)
        self.assertEqual(Partner.objects.filter(name__startswith="DEMO ONLY -").count(), 2)
        self.assertEqual(Document.objects.filter(title__startswith="DEMO ONLY -").count(), 1)
        self.assertEqual(MOU.objects.filter(reference="DEMO-ONLY-MOU-001").count(), 1)
        self.assertEqual(MonthlyCheckIn.objects.count(), 1)
        self.assertEqual(Interaction.objects.count(), 1)
        self.assertEqual(FollowUpTask.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(resource="demo.seed").count(), 1)

        image = PartnerImage.objects.get(
            partner__name__endswith="Acacia Innovation Cooperative"
        )
        self.assertTrue(image.file.name.endswith(".png"))
        with self.assertRaises(ValueError):
            image.file.url

    def test_seed_requires_existing_active_actor(self):
        with self.assertRaisesMessage(
            CommandError, "An active local account with that email"
        ):
            call_command(
                "seed_demo_data",
                actor_email="missing.demo.user@gmail.com",
                stdout=StringIO(),
            )
