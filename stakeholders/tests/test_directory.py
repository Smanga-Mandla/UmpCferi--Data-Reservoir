from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role, TOTPDevice
from incubation.models import Incubatee
from partners.models import Partner, PartnerRepresentative
from stakeholders.models import (
    BoardMemberProfile,
    POPIAConsent,
    StaffIncubateeAssignment,
    Stakeholder,
    StakeholderTypeAssignment,
)


User = get_user_model()


class StakeholderDirectoryTests(TestCase):
    def setUp(self):
        self.admin = self.create_user("directory-admin@gmail.com", Role.Code.ADMIN)
        self.client.force_login(self.admin)
        session = self.client.session
        session["mfa_verified_user"] = self.admin.pk
        session.save()

    def create_user(self, email, role_code):
        user = User.objects.create_user(email=email, password="test-only-password")
        user.roles.add(Role.objects.get(code=role_code))
        if role_code in {
            Role.Code.SUPER_ADMIN,
            Role.Code.SYSTEM_ADMIN,
            Role.Code.ADMIN,
            Role.Code.BOARD_MEMBER,
        }:
            TOTPDevice.objects.create(
                user=user,
                secret_encrypted="test-only-encrypted-secret",
                confirmed=True,
            )
        return user

    def create_stakeholder(self, **kwargs):
        return Stakeholder.objects.create(
            name=kwargs.pop("name", "Synthetic Stakeholder"),
            email=kwargs.pop("email", "person@gmail.com"),
            phone=kwargs.pop("phone", "+1 (555) 000-1111"),
            owner=self.admin,
            created_by=self.admin,
            **kwargs,
        )

    def test_duplicate_email_is_blocked_and_phone_match_warns(self):
        existing = self.create_stakeholder()
        duplicate_email = self.client.post(
            reverse("stakeholders:create"),
            {
                "name": "Second synthetic person",
                "title": "",
                "email": "PERSON@gmail.com",
                "phone": "",
                "sensitivity": "internal",
                "relationship_types": [],
            },
        )
        self.assertEqual(duplicate_email.status_code, 200)
        self.assertContains(duplicate_email, "Possible duplicate stakeholder")
        self.assertContains(duplicate_email, existing.name)
        self.assertEqual(Stakeholder.objects.count(), 1)

        phone_match = self.client.post(
            reverse("stakeholders:create"),
            {
                "name": "Other synthetic person",
                "title": "",
                "email": "other@gmail.com",
                "phone": "+1-555-000-1111",
                "sensitivity": "internal",
                "relationship_types": [],
            },
            follow=True,
        )
        self.assertEqual(phone_match.status_code, 200)
        self.assertContains(phone_match, "matches existing contact details")

    def test_person_record_is_reused_for_multiple_partner_relationships(self):
        stakeholder = self.create_stakeholder()
        partners = [
            Partner.objects.create(
                name=f"Synthetic partner {index}",
                organisation_type=Partner.OrganisationType.NGO,
                owner=self.admin,
                created_by=self.admin,
            )
            for index in (1, 2)
        ]
        user_count = User.objects.count()
        for partner in partners:
            response = self.client.post(
                reverse("partners:representative_add", args=(partner.pk,)),
                {"stakeholder": stakeholder.pk},
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PartnerRepresentative.objects.filter(
                stakeholder=stakeholder, is_active=True
            ).count(),
            2,
        )
        self.assertEqual(
            StakeholderTypeAssignment.objects.filter(
                stakeholder=stakeholder,
                relationship_type=Stakeholder.RelationshipType.PARTNER_REPRESENTATIVE,
                is_active=True,
            ).count(),
            1,
        )
        self.assertEqual(User.objects.count(), user_count)

    def test_board_profile_and_consent_history_are_saved(self):
        response = self.client.post(
            reverse("stakeholders:create"),
            {
                "name": "Synthetic Board Contact",
                "title": "Board member",
                "email": "board-contact@gmail.com",
                "phone": "",
                "sensitivity": "internal",
                "relationship_types": [Stakeholder.RelationshipType.BOARD_MEMBER],
                "appointment_date": "2026-01-10",
                "term_end": "2028-01-10",
                "committees": "Audit",
                "declarations_of_interest": "No declarations recorded",
                "position": "",
                "contract_start": "",
                "contract_end": "",
                "skills": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        stakeholder = Stakeholder.objects.get(email="board-contact@gmail.com")
        profile = BoardMemberProfile.objects.get(stakeholder=stakeholder)
        self.assertEqual(profile.committees, "Audit")
        self.assertEqual(profile.term_end.isoformat(), "2028-01-10")

        consent_response = self.client.post(
            reverse("stakeholders:consent_add", args=(stakeholder.pk,)),
            {
                "consent_date": "2026-10-20",
                "purpose": "Contact about services",
                "method": "Signed form",
            },
        )
        self.assertEqual(consent_response.status_code, 302)
        consent = POPIAConsent.objects.get(stakeholder=stakeholder)
        self.assertEqual(consent.recorded_by, self.admin)
        self.assertEqual(consent.purpose, "Contact about services")
        with self.assertRaises(ValidationError):
            consent.save()
        with self.assertRaises(PermissionDenied):
            consent.delete()

    def test_staff_profile_incubatee_assignments_are_scoped_and_historical(self):
        assigned = Incubatee.objects.create(
            legal_name="Synthetic Assigned Company",
            created_by=self.admin,
        )
        second = Incubatee.objects.create(
            legal_name="Synthetic Former Assignment",
            created_by=self.admin,
        )
        response = self.client.post(
            reverse("stakeholders:create"),
            {
                "name": "Synthetic Staff Profile",
                "title": "Programme Officer",
                "email": "staff-profile@gmail.com",
                "phone": "",
                "sensitivity": "internal",
                "relationship_types": [Stakeholder.RelationshipType.STAFF],
                "position": "Programme Officer",
                "contract_start": "",
                "contract_end": "",
                "skills": "Business coaching",
                "assigned_incubatees": [assigned.pk, second.pk],
            },
        )
        self.assertEqual(response.status_code, 302)
        stakeholder = Stakeholder.objects.get(email="staff-profile@gmail.com")
        profile = stakeholder.staff_profile
        self.assertSetEqual(
            set(profile.assigned_incubatees.values_list("pk", flat=True)),
            {assigned.pk, second.pk},
        )
        self.assertEqual(profile.incubatee_assignments.filter(is_active=True).count(), 2)

        response = self.client.post(
            reverse("stakeholders:update", args=(stakeholder.pk,)),
            {
                "name": stakeholder.name,
                "title": stakeholder.title,
                "email": stakeholder.email,
                "phone": "",
                "sensitivity": "internal",
                "relationship_types": [Stakeholder.RelationshipType.STAFF],
                "position": profile.position,
                "contract_start": "",
                "contract_end": "",
                "skills": profile.skills,
                "assigned_incubatees": [assigned.pk],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertSetEqual(
            set(profile.assigned_incubatees.values_list("pk", flat=True)),
            {assigned.pk},
        )
        ended = StaffIncubateeAssignment.objects.get(
            staff_profile=profile,
            incubatee=second,
            is_active=False,
        )
        self.assertEqual(ended.ended_by, self.admin)
        self.assertIsNotNone(ended.ended_at)
        with self.assertRaises(PermissionDenied):
            ended.delete()
        with self.assertRaises(PermissionDenied):
            StaffIncubateeAssignment.objects.filter(pk=ended.pk).update(
                ended_at=None
            )

        detail = self.client.get(
            reverse("stakeholders:detail", args=(stakeholder.pk,))
        )
        self.assertContains(detail, assigned.legal_name)
        self.assertNotContains(detail, second.legal_name)

    def test_staff_profile_cannot_be_assigned_restricted_incubatee_without_access(self):
        restricted = Incubatee.objects.create(
            legal_name="Synthetic Restricted Assignment",
            created_by=self.admin,
            sensitivity=Incubatee.Sensitivity.RESTRICTED,
        )
        response = self.client.post(
            reverse("stakeholders:create"),
            {
                "name": "Synthetic Staff Restricted Assignment",
                "title": "Programme Officer",
                "email": "restricted-assignment@gmail.com",
                "phone": "",
                "sensitivity": "internal",
                "relationship_types": [Stakeholder.RelationshipType.STAFF],
                "position": "",
                "contract_start": "",
                "contract_end": "",
                "skills": "",
                "assigned_incubatees": [restricted.pk],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(
            StaffIncubateeAssignment.objects.filter(incubatee=restricted).exists()
        )

    def test_sensitive_contact_and_profile_fields_are_role_restricted(self):
        stakeholder = self.create_stakeholder(
            email="private-contact@gmail.com",
            sensitivity="public",
        )
        StakeholderTypeAssignment.objects.create(
            stakeholder=stakeholder,
            relationship_type=Stakeholder.RelationshipType.BOARD_MEMBER,
            assigned_by=self.admin,
        )
        BoardMemberProfile.objects.create(
            stakeholder=stakeholder,
            committees="Restricted synthetic committee",
            declarations_of_interest="Restricted synthetic declaration",
        )

        board_member = self.create_user(
            "board-viewer@gmail.com", Role.Code.BOARD_MEMBER
        )
        self.client.force_login(board_member)
        session = self.client.session
        session["mfa_verified_user"] = board_member.pk
        session.save()
        response = self.client.get(
            reverse("stakeholders:detail", args=(stakeholder.pk,))
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, stakeholder.email)
        self.assertNotContains(response, "Restricted synthetic declaration")
        self.assertEqual(
            self.client.get(
                reverse("stakeholders:consent_add", args=(stakeholder.pk,))
            ).status_code,
            403,
        )

        system_admin = self.create_user(
            "system-viewer@gmail.com", Role.Code.SYSTEM_ADMIN
        )
        self.client.force_login(system_admin)
        session = self.client.session
        session["mfa_verified_user"] = system_admin.pk
        session.save()
        response = self.client.get(
            reverse("stakeholders:detail", args=(stakeholder.pk,))
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, stakeholder.email)
        self.assertNotContains(response, "Restricted synthetic declaration")

        self.client.force_login(self.admin)
        session = self.client.session
        session["mfa_verified_user"] = self.admin.pk
        session.save()
        response = self.client.get(
            reverse("stakeholders:detail", args=(stakeholder.pk,))
        )
        self.assertContains(response, stakeholder.email)
        self.assertContains(response, "Restricted synthetic declaration")

    def test_user_without_stakeholder_grant_is_denied_record_access(self):
        stakeholder = self.create_stakeholder()
        user = User.objects.create_user(
            email="no-role@gmail.com",
            password="test-only-password",
        )
        self.client.force_login(user)
        response = self.client.get(
            reverse("stakeholders:detail", args=(stakeholder.pk,))
        )
        self.assertEqual(response.status_code, 403)

    def test_stakeholder_type_assignment_and_profile_edit(self):
        stakeholder = self.create_stakeholder()
        response = self.client.post(
            reverse("stakeholders:update", args=(stakeholder.pk,)),
            {
                "name": stakeholder.name,
                "title": "Senior contact",
                "email": stakeholder.email,
                "phone": stakeholder.phone,
                "sensitivity": "internal",
                "relationship_types": [
                    Stakeholder.RelationshipType.STAFF,
                    Stakeholder.RelationshipType.FREELANCER,
                ],
                "appointment_date": "",
                "term_end": "",
                "committees": "",
                "declarations_of_interest": "",
                "position": "Programme Adviser",
                "contract_start": "2026-01-01",
                "contract_end": "2026-12-31",
                "skills": "Mentoring",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(
                stakeholder.type_assignments.filter(is_active=True).values_list(
                    "relationship_type", flat=True
                )
            ),
            {
                Stakeholder.RelationshipType.STAFF,
                Stakeholder.RelationshipType.FREELANCER,
            },
        )
        self.assertEqual(stakeholder.staff_profile.position, "Programme Adviser")
