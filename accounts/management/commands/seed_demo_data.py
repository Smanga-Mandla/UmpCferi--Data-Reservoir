from datetime import date, datetime, timedelta
from io import BytesIO

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.utils import timezone
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas

from accounts.models import User
from audit.models import AuditEvent
from documents.models import Document, DocumentVersion, ExpiryAlert
from incubation.models import (
    Cohort,
    Incubatee,
    IncubateeBaselineSnapshot,
    IncubateeConsent,
    IncubateeDocument,
    IncubateeFounder,
    IncubateeMetricObservation,
    IncubateeMilestone,
    IncubateeStatusHistory,
    FounderProfile,
    MonthlyCheckIn,
    MentoringSession,
    Programme,
    ProgrammeCostObservation,
    QuarterlyReview,
)
from interactions.models import (
    EventAttendance,
    FollowUpTask,
    Interaction,
    InteractionHistory,
    InteractionParticipant,
)
from partners.models import (
    MOU,
    MOUAttachment,
    MOUExpiryAlert,
    MOUObligation,
    MOUHistory,
    Partner,
    PartnerImage,
    PartnerRepresentative,
)
from stakeholders.models import (
    BoardMemberProfile,
    POPIAConsent,
    StaffIncubateeAssignment,
    StaffProfile,
    Stakeholder,
    StakeholderTypeAssignment,
)


DEMO_PREFIX = "DEMO ONLY - "


class Command(BaseCommand):
    help = "Add clearly marked synthetic sample records to a local development database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--actor-email",
            required=True,
            help="Email of an existing active account in the local database.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data may only be seeded with development settings.")

        database = connections["default"]
        host = database.settings_dict.get("HOST", "")
        if database.vendor != "sqlite" and host not in {"", "localhost", "127.0.0.1", "::1"}:
            raise CommandError(
                "Refusing to seed a non-local database. Point development settings at a local "
                "database first; this command never seeds remote databases."
            )

        actor = User.objects.filter(
            email__iexact=options["actor_email"].strip(),
            is_active=True,
        ).first()
        if actor is None:
            raise CommandError(
                "An active local account with that email is required. No account was created."
            )

        with transaction.atomic():
            self._seed(actor)

        self.stdout.write(
            self.style.SUCCESS(
                "Synthetic demo data is ready. Records are marked 'DEMO ONLY -'; "
                "partner images and sample documents are stored in private media."
            )
        )

    def _seed(self, actor):
        today = timezone.localdate()
        month_start = today.replace(day=1)
        quarter_start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)

        programme, _ = Programme.objects.get_or_create(
            name=f"{DEMO_PREFIX} Enterprise Growth Programme",
            defaults={
                "description": (
                    "Synthetic sample programme for exercising the incubation and reporting "
                    "screens. This is not a real CFERI programme."
                ),
                "is_active": True,
            },
        )
        cohort, _ = Cohort.objects.get_or_create(
            programme=programme,
            name=f"{DEMO_PREFIX} 2026 Cohort",
            defaults={
                "start_date": date(today.year, 1, 1),
                "end_date": date(today.year, 12, 31),
                "is_active": True,
            },
        )

        partner, _ = Partner.objects.get_or_create(
            name=f"{DEMO_PREFIX} Acacia Innovation Cooperative",
            defaults={
                "organisation_type": Partner.OrganisationType.ACADEMIC,
                "sector": "Synthetic demonstration",
                "province": "Mpumalanga",
                "website": "https://example.test/acacia",
                "created_by": actor,
            },
        )
        second_partner, _ = Partner.objects.get_or_create(
            name=f"{DEMO_PREFIX} Baobab Community Fund",
            defaults={
                "organisation_type": Partner.OrganisationType.FUNDER,
                "sector": "Synthetic demonstration",
                "province": "Mpumalanga",
                "website": "https://example.test/baobab",
                "created_by": actor,
            },
        )

        board_contact = self._stakeholder(
            actor,
            "DEMO ONLY - Board Contact",
            "demo.board@example.test",
            Stakeholder.RelationshipType.BOARD_MEMBER,
        )
        staff_contact = self._stakeholder(
            actor,
            "DEMO ONLY - Programme Coordinator",
            "demo.staff@example.test",
            Stakeholder.RelationshipType.STAFF,
        )
        founder_contact = self._stakeholder(
            actor,
            "DEMO ONLY - Sample Founder",
            "demo.founder@example.test",
            Stakeholder.RelationshipType.FOUNDER,
        )
        representative, _ = Stakeholder.objects.get_or_create(
            email="demo.partner@example.test",
            defaults={
                "name": "DEMO ONLY - Partner Representative",
                "title": "Sample Liaison",
                "created_by": actor,
            },
        )
        self._stakeholder_type(
            representative, actor, Stakeholder.RelationshipType.PARTNER_REPRESENTATIVE
        )
        self._create_related(
            PartnerRepresentative,
            {"partner": partner, "stakeholder": representative},
            {"linked_by": actor, "is_active": True},
        )
        self._create_related(
            PartnerRepresentative,
            {"partner": second_partner, "stakeholder": board_contact},
            {"linked_by": actor, "is_active": True},
        )
        BoardMemberProfile.objects.get_or_create(
            stakeholder=board_contact,
            defaults={
                "appointment_date": date(today.year - 1, 1, 1),
                "term_end": date(today.year + 2, 12, 31),
                "committees": "DEMO ONLY - Governance committee",
                "declarations_of_interest": "Synthetic example; no actual interests recorded.",
            },
        )
        staff_profile, _ = StaffProfile.objects.get_or_create(
            stakeholder=staff_contact,
            defaults={
                "position": "DEMO ONLY - Programme Coordinator",
                "contract_start": date(today.year, 1, 1),
                "skills": "Synthetic sample skills",
            },
        )
        self._create_related(
            POPIAConsent,
            {"stakeholder": founder_contact, "purpose": "Synthetic data demonstration"},
            {
                "consent_date": today,
                "method": "Synthetic demonstration record",
                "recorded_by": actor,
            },
        )
        self._create_related(
            POPIAConsent,
            {"stakeholder": representative, "purpose": "Synthetic data demonstration"},
            {
                "consent_date": today,
                "method": "Synthetic demonstration record",
                "recorded_by": actor,
            },
        )

        incubatee, created = Incubatee.objects.get_or_create(
            legal_name=f"{DEMO_PREFIX} Cedar Works (Synthetic)",
            defaults={
                "trading_name": f"{DEMO_PREFIX} Cedar Works",
                "entity_type": Incubatee.EntityType.COOPERATIVE,
                "vat_status": Incubatee.VATStatus.NOT_REGISTERED,
                "province": "Mpumalanga",
                "municipality": "Demo Municipality",
                "urban_rural": Incubatee.UrbanRural.URBAN,
                "sub_sector": "Synthetic sustainable products",
                "products_services": "Fictional sample products for demonstrating system screens.",
                "business_model": Incubatee.BusinessModel.B2B,
                "business_stage": Incubatee.BusinessStage.EARLY_REVENUE,
                "formality": Incubatee.Formality.FORMAL,
                "full_time_employees": 2,
                "part_time_employees": 1,
                "revenue_12_months_zar": 84000,
                "monthly_average_revenue_zar": 7000,
                "funding_received_zar": 12000,
                "funding_sources": "Synthetic demonstration grant",
                "debt_zar": 0,
                "has_bank_account": True,
                "customer_count": 8,
                "top_customer_types": ["Synthetic local retailers"],
                "exports": False,
                "finance_score": 3,
                "marketing_score": 3,
                "operations_score": 4,
                "compliance_score": 3,
                "technology_score": 3,
                "leadership_score": 4,
                "top_support_needs": ["Bookkeeping", "Market access"],
                "status": Incubatee.Status.ACTIVE,
                "programme": programme,
                "cohort": cohort,
                "staff_owner": actor,
                "created_by": actor,
                "updated_by": actor,
                "source": "synthetic_demo",
            },
        )
        founder_profile, _ = FounderProfile.objects.get_or_create(
            stakeholder=founder_contact,
            defaults={
                "highest_qualification": "Synthetic sample qualification",
                "prior_business_experience": "Synthetic example only.",
            },
        )
        self._create_related(
            IncubateeFounder,
            {"incubatee": incubatee, "founder": founder_profile},
            {"ownership_percentage": 100, "is_active": True},
        )
        self._create_related(
            IncubateeConsent,
            {
                "incubatee": incubatee,
                "purpose": IncubateeConsent.Purpose.POPIA,
            },
            {
                "status": Incubatee.ConsentStatus.GRANTED,
                "consent_date": today,
                "method": "Synthetic demonstration record",
                "recorded_by": actor,
            },
        )
        if created:
            IncubateeStatusHistory.objects.create(
                incubatee=incubatee,
                old_status="",
                new_status=incubatee.status,
                changed_by=actor,
            )
        IncubateeBaselineSnapshot.objects.get_or_create(
            incubatee=incubatee,
            source="synthetic_demo",
            defaults={
                "snapshot": {
                    "status": incubatee.status,
                    "employees": 3,
                    "monthly_revenue_zar": 7000,
                },
                "founder_snapshot": [
                    {"name": founder_contact.name, "ownership_percentage": "100.00"}
                ],
                "changed_by": actor,
            },
        )
        self._create_related(
            StaffIncubateeAssignment,
            {"staff_profile": staff_profile, "incubatee": incubatee},
            {"assigned_by": actor, "is_active": True},
        )

        mou, _ = MOU.objects.get_or_create(
            reference="DEMO-ONLY-MOU-001",
            defaults={
                "partner": partner,
                "title": f"{DEMO_PREFIX} Sample Innovation Partnership",
                "purpose": "Synthetic example agreement for testing MOU workflows.",
                "signing_date": today - timedelta(days=30),
                "start_date": today - timedelta(days=30),
                "end_date": today + timedelta(days=180),
                "renewal_terms": "Synthetic example renewal terms.",
                "value_zar": 25000,
                "status": MOU.Status.ACTIVE,
                "cferi_signatory": "DEMO ONLY - Sample Signatory",
                "primary_representative": representative,
                "created_by": actor,
            },
        )
        MOUHistory.objects.get_or_create(
            mou=mou,
            snapshot=mou.snapshot(),
            defaults={"changed_by": actor},
        )
        self._create_related(
            MOUObligation,
            {"mou": mou, "title": f"{DEMO_PREFIX} Share quarterly progress summary"},
            {
                "description": "Synthetic example obligation; no real partner deliverable.",
                "owner": actor,
                "due_date": today + timedelta(days=30),
                "status": MOUObligation.Status.OPEN,
                "created_by": actor,
            },
        )
        MOUExpiryAlert.objects.get_or_create(
            mou=mou,
            end_date=mou.end_date,
            days_before=90,
            defaults={"due_date": mou.end_date - timedelta(days=90)},
        )

        self._ensure_partner_image(partner, actor, (37, 91, 74))
        self._ensure_partner_image(second_partner, actor, (112, 83, 50))

        policy_doc, _ = Document.objects.get_or_create(
            title=f"{DEMO_PREFIX} Sample Document Retention Guide",
            defaults={
                "category": Document.Category.POLICIES,
                "reference_number": "DEMO-ONLY-DOC-001",
                "issuing_authority": "Synthetic demonstration",
                "issue_date": today - timedelta(days=30),
                "expiry_date": today + timedelta(days=90),
                "notes": "Synthetic sample document. Not an approved CFERI policy.",
                "tags": ["demo", "synthetic"],
                "sensitivity": Document.Sensitivity.INTERNAL,
                "owner": actor,
                "created_by": actor,
            },
        )
        self._ensure_document_version(policy_doc, actor)
        ExpiryAlert.objects.get_or_create(
            document=policy_doc,
            expiry_date=policy_doc.expiry_date,
            days_before=60,
            defaults={"due_date": policy_doc.expiry_date - timedelta(days=60)},
        )
        self._ensure_mou_attachment(mou, actor)

        self._create_related(
            IncubateeConsent,
            {
                "incubatee": incubatee,
                "purpose": IncubateeConsent.Purpose.FUNDER_REPORTING,
            },
            {
                "status": Incubatee.ConsentStatus.GRANTED,
                "consent_date": today,
                "method": "Synthetic demonstration record",
                "recorded_by": actor,
            },
        )
        checkin, _ = MonthlyCheckIn.objects.get_or_create(
            incubatee=incubatee,
            period_start=month_start,
            defaults={
                "due_date": month_start + timedelta(days=14),
                "sent_at": timezone.now(),
                "submitted_at": timezone.now(),
                "submitted_by": actor,
                "completeness_score": 90,
            },
        )
        metric, _ = IncubateeMetricObservation.objects.get_or_create(
            incubatee=incubatee,
            period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
            period_start=month_start,
            metric=IncubateeMetricObservation.Metric.REVENUE_ZAR,
            defaults={
                "decimal_value": 7000,
                "availability": IncubateeMetricObservation.Availability.RECORDED,
                "source": "synthetic_demo",
                "monthly_checkin": checkin,
                "recorded_by": actor,
            },
        )
        self._ensure_checkin_evidence(checkin, metric, incubatee, actor)
        quarterly_review, _ = QuarterlyReview.objects.get_or_create(
            incubatee=incubatee,
            period_start=quarter_start,
            defaults={
                "tax_clearance_status": "synthetic_example_current",
                "cipc_annual_return_status": "synthetic_example_current",
                "submitted_by": actor,
                "source": "synthetic_demo",
            },
        )
        self._ensure_quarterly_review_file(quarterly_review, actor)
        IncubateeMilestone.objects.get_or_create(
            incubatee=incubatee,
            milestone_type=IncubateeMilestone.Type.FIRST_SALE,
            milestone_date=today - timedelta(days=14),
            defaults={
                "description": "Synthetic first-sale milestone for dashboard demonstrations.",
                "value_zar": 1500,
                "source": "synthetic_demo",
                "recorded_by": actor,
            },
        )
        MentoringSession.objects.get_or_create(
            incubatee=incubatee,
            mentor=representative,
            session_date=today - timedelta(days=7),
            defaults={
                "mentoring_hours": 1.5,
                "topics": "Synthetic planning and bookkeeping discussion.",
                "progress_rating": 4,
                "source": "synthetic_demo",
                "recorded_by": actor,
            },
        )
        ProgrammeCostObservation.objects.get_or_create(
            programme=programme,
            period_start=month_start,
            source="synthetic_demo",
            defaults={"amount_zar": 5000, "recorded_by": actor},
        )
        self._ensure_incubatee_document(incubatee, actor)

        interaction, created = Interaction.objects.get_or_create(
            occurred_at=timezone.make_aware(
                datetime.combine(today - timedelta(days=3), datetime.min.time())
            ),
            interaction_type=Interaction.Type.WORKSHOP,
            created_by=actor,
            defaults={
                "duration_minutes": 90,
                "topics": "DEMO ONLY - Business planning and record keeping",
                "outcome": "Synthetic example workshop outcome.",
                "owner": actor,
            },
        )
        interaction.incubatees.add(incubatee)
        interaction.partners.add(partner)
        self._create_related(
            InteractionParticipant,
            {"interaction": interaction, "stakeholder": founder_contact},
            {},
        )
        EventAttendance.objects.get_or_create(
            interaction=interaction,
            stakeholder=founder_contact,
            defaults={
                "status": EventAttendance.Status.ATTENDED,
                "recorded_by": actor,
            },
        )
        FollowUpTask.objects.get_or_create(
            interaction=interaction,
            description=f"{DEMO_PREFIX} Share sample workshop notes",
            defaults={
                "owner": actor,
                "due_date": today + timedelta(days=7),
                "status": FollowUpTask.Status.OPEN,
                "created_by": actor,
            },
        )
        if created:
            InteractionHistory.objects.create(
                interaction=interaction,
                change_type=InteractionHistory.ChangeType.CREATED,
                snapshot={
                    "interaction_type": interaction.interaction_type,
                    "topics": interaction.topics,
                    "outcome": interaction.outcome,
                    "demo": True,
                },
                changed_by=actor,
            )

        AuditEvent.objects.get_or_create(
            actor=actor,
            action=AuditEvent.Action.CREATE,
            resource="demo.seed",
            object_id="synthetic-demo-dataset",
            defaults={
                "metadata": {
                    "dataset": "synthetic",
                    "scope": "local development",
                    "notice": "No real CFERI data.",
                },
            },
        )

    def _stakeholder(self, actor, name, email, relationship):
        stakeholder, _ = Stakeholder.objects.get_or_create(
            email=email,
            defaults={"name": name, "created_by": actor},
        )
        self._stakeholder_type(stakeholder, actor, relationship)
        return stakeholder

    @staticmethod
    def _stakeholder_type(stakeholder, actor, relationship):
        StakeholderTypeAssignment.objects.get_or_create(
            stakeholder=stakeholder,
            relationship_type=relationship,
            is_active=True,
            defaults={"assigned_by": actor},
        )

    @staticmethod
    def _create_related(model, lookup, defaults):
        instance, _ = model.objects.get_or_create(**lookup, defaults=defaults)
        return instance

    @staticmethod
    def _ensure_partner_image(partner, actor, color):
        if partner.images.exists():
            return
        image = Image.new("RGB", (640, 360), color)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((24, 24, 616, 336), radius=24, outline="white", width=5)
        draw.text((50, 135), "DEMO ONLY", fill="white", stroke_width=1, stroke_fill=color)
        draw.text((50, 190), partner.name.removeprefix(DEMO_PREFIX), fill="white")
        output = BytesIO()
        image.save(output, format="PNG")
        logo = PartnerImage(partner=partner, uploaded_by=actor)
        logo.file.save("synthetic-demo-partner.png", ContentFile(output.getvalue()), save=True)

    @staticmethod
    def _pdf_bytes(title):
        output = BytesIO()
        pdf = canvas.Canvas(output, pagesize=(612, 792))
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(72, 720, "SYNTHETIC DEMO DATA")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, 680, title)
        pdf.drawString(72, 650, "Fictional sample only. Not an actual CFERI record.")
        pdf.save()
        return output.getvalue()

    def _ensure_document_version(self, document, actor):
        if document.versions.exists():
            return
        content = self._pdf_bytes(document.title)
        version = DocumentVersion(
            document=document,
            original_filename="synthetic-demo-document.pdf",
            content_type="application/pdf",
            size_bytes=len(content),
            version_number=1,
            change_note="Synthetic demonstration file.",
            metadata_snapshot={"demo": True},
            is_current=True,
            uploaded_by=actor,
        )
        version.file.save("synthetic-demo-document.pdf", ContentFile(content), save=True)

    def _ensure_mou_attachment(self, mou, actor):
        if mou.attachments.exists():
            return
        content = self._pdf_bytes(mou.title)
        attachment = MOUAttachment(
            mou=mou,
            category=MOUAttachment.Category.SIGNED_MOU,
            title=f"{DEMO_PREFIX} Sample agreement attachment",
            original_filename="synthetic-demo-agreement.pdf",
            uploaded_by=actor,
        )
        attachment.file.save("synthetic-demo-agreement.pdf", ContentFile(content), save=True)

    def _ensure_incubatee_document(self, incubatee, actor):
        if incubatee.documents.exists():
            return
        content = self._pdf_bytes(incubatee.trading_name)
        document = IncubateeDocument(
            incubatee=incubatee,
            category=IncubateeDocument.Category.BUSINESS_PLAN,
            title=f"{DEMO_PREFIX} Sample business plan",
            original_filename="synthetic-demo-business-plan.pdf",
            content_type="application/pdf",
            size_bytes=len(content),
            uploaded_by=actor,
        )
        document.file.save("synthetic-demo-business-plan.pdf", ContentFile(content), save=True)

    def _ensure_quarterly_review_file(self, review, actor):
        if review.management_accounts:
            return
        content = self._pdf_bytes("Synthetic quarterly review summary")
        review.original_filename = "synthetic-demo-quarterly-review.pdf"
        review.content_type = "application/pdf"
        review.size_bytes = len(content)
        review.submitted_by = actor
        review.management_accounts.save(
            "synthetic-demo-quarterly-review.pdf",
            ContentFile(content),
            save=True,
        )

    def _ensure_checkin_evidence(self, checkin, observation, incubatee, actor):
        from incubation.models import MonthlyCheckInEvidence

        if checkin.evidence.exists():
            return
        content = self._pdf_bytes("Synthetic monthly check-in evidence")
        evidence = MonthlyCheckInEvidence(
            checkin=checkin,
            observation=observation,
            incubatee=incubatee,
            title=f"{DEMO_PREFIX} Monthly revenue evidence",
            original_filename="synthetic-demo-checkin-evidence.pdf",
            content_type="application/pdf",
            size_bytes=len(content),
        )
        evidence.file.save("synthetic-demo-checkin-evidence.pdf", ContentFile(content), save=True)
