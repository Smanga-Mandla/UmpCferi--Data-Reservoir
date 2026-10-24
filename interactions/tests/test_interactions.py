from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, TOTPDevice
from incubation.models import Incubatee
from interactions.models import (
    EventAttendance,
    FollowUpTask,
    Interaction,
    InteractionHistory,
)
from partners.models import Partner
from stakeholders.models import Stakeholder

User = get_user_model()


class InteractionWorkflowTests(TestCase):
    def setUp(self):
        self.admin = self.create_user("phase8-admin@gmail.com", Role.Code.ADMIN)
        self.staff = self.create_user("phase8-staff@gmail.com", Role.Code.CFERI_STAFF)
        self.board = self.create_user("phase8-board@gmail.com", Role.Code.BOARD_MEMBER)
        self.freelancer = self.create_user("phase8-freelancer@gmail.com", Role.Code.FREELANCER)
        self.system_admin = self.create_user(
            "phase8-system-admin@gmail.com", Role.Code.SYSTEM_ADMIN
        )
        self.client.force_login(self.admin)
        self.verify_mfa(self.admin)
        self.incubatee = Incubatee.objects.create(
            legal_name="Synthetic Phase 8 Company",
            created_by=self.admin,
            owner=self.admin,
            staff_owner=self.staff,
        )
        self.incubatee.assigned_users.add(self.staff)
        self.partner = Partner.objects.create(
            name="Synthetic Phase 8 Partner",
            organisation_type=Partner.OrganisationType.ACADEMIC,
            created_by=self.admin,
            owner=self.admin,
        )
        self.stakeholder = Stakeholder.objects.create(
            name="Synthetic Phase 8 Participant",
            email="participant-phase8@gmail.com",
            created_by=self.admin,
            owner=self.admin,
        )

    def create_user(self, email, role_code):
        user = User.objects.create_user(
            email=email,
            password="synthetic-test-password",
            access_expires_at=(
                timezone.now() + timedelta(days=5)
                if role_code == Role.Code.FREELANCER
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

    def login_as(self, user):
        self.client.force_login(user)
        if user.has_mandatory_mfa_role:
            self.verify_mfa(user)

    def create_interaction(self, **kwargs):
        interaction_type = kwargs.pop("interaction_type", Interaction.Type.MEETING)
        duration_minutes = kwargs.pop("duration_minutes", 30)
        return Interaction.objects.create(
            occurred_at=timezone.make_aware(datetime(2026, 10, 20, 10, 30)),
            duration_minutes=duration_minutes,
            interaction_type=interaction_type,
            created_by=self.admin,
            owner=self.admin,
            **kwargs,
        )

    def create_via_form(self, *, interaction_type="meeting", duration="30"):
        return self.client.post(
            reverse("interactions:create"),
            {
                "occurred_at": "2026-10-20T10:30",
                "duration_minutes": duration,
                "interaction_type": interaction_type,
                "participants": [str(self.stakeholder.pk)],
                "additional_participants": "Unlisted Synthetic Guest",
                "incubatees": [str(self.incubatee.pk)],
                "partners": [str(self.partner.pk)],
                "topics": "Synthetic planning discussion",
                "outcome": "Synthetic follow-up agreed",
                "sensitivity": Interaction.Sensitivity.INTERNAL,
            },
        )

    def test_create_update_participants_links_duration_and_history(self):
        response = self.create_via_form()
        interaction = Interaction.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(interaction.duration_minutes, 30)
        self.assertEqual(list(interaction.participants.all()), [self.stakeholder])
        self.assertTrue(
            interaction.participant_links.filter(
                stakeholder__isnull=True,
                display_name="Unlisted Synthetic Guest",
            ).exists()
        )
        self.assertEqual(list(interaction.incubatees.all()), [self.incubatee])
        self.assertEqual(list(interaction.partners.all()), [self.partner])
        self.assertIn(self.staff, interaction.assigned_users.all())
        self.assertEqual(
            list(interaction.history.values_list("change_type", flat=True)),
            [InteractionHistory.ChangeType.CREATED],
        )

        response = self.client.post(
            reverse("interactions:update", args=(interaction.pk,)),
            {
                "occurred_at": "2026-10-20T11:00",
                "duration_minutes": "0",
                "interaction_type": Interaction.Type.MENTORING,
                "participants": [str(self.stakeholder.pk)],
                "incubatees": [str(self.incubatee.pk)],
                "partners": [str(self.partner.pk)],
                "topics": "Updated synthetic topics",
                "outcome": "Updated synthetic result",
                "sensitivity": Interaction.Sensitivity.INTERNAL,
            },
        )
        interaction.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(interaction.duration_minutes, 0)
        self.assertEqual(interaction.interaction_type, Interaction.Type.MENTORING)
        self.assertEqual(interaction.history.count(), 2)

        incubatee_detail = self.client.get(
            reverse("incubation:detail", args=(self.incubatee.pk,))
        )
        self.assertEqual(incubatee_detail.status_code, 200)
        self.assertTrue(
            any(
                event["kind"] == "Interaction"
                and event["url"] == reverse("interactions:detail", args=(interaction.pk,))
                for event in incubatee_detail.context["timeline"]
            )
        )

    def test_duration_validation_rejects_negative_value(self):
        response = self.create_via_form(duration="-1")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Interaction.objects.exists())
        self.assertContains(response, "Ensure this value is greater than or equal to 0.")

    def test_restricted_sensitivity_cannot_be_created_by_admin(self):
        response = self.client.post(
            reverse("interactions:create"),
            {
                "occurred_at": "2026-10-20T10:30",
                "duration_minutes": "0",
                "interaction_type": Interaction.Type.MEETING,
                "topics": "Synthetic restricted topic",
                "sensitivity": Interaction.Sensitivity.RESTRICTED,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Interaction.objects.exists())
        self.assertContains(response, "Restricted interactions can only be recorded by a Super Admin.")

    def test_filters_apply_type_date_and_linked_entity(self):
        matching = self.create_interaction(
            interaction_type=Interaction.Type.WORKSHOP,
        )
        matching.incubatees.add(self.incubatee)
        other = self.create_interaction(interaction_type=Interaction.Type.CALL)
        other.occurred_at = timezone.make_aware(datetime(2026, 9, 1, 10, 30))
        other.save()

        response = self.client.get(
            reverse("interactions:list"),
            {
                "interaction_type": Interaction.Type.WORKSHOP,
                "date_from": "2026-10-01",
                "date_to": "2026-10-31",
                "incubatee": str(self.incubatee.pk),
            },
        )
        self.assertEqual(
            [row["interaction"].pk for row in response.context["rows"]],
            [matching.pk],
        )

    def test_system_admin_has_no_interaction_access_without_a_grant(self):
        interaction = self.create_interaction()
        self.login_as(self.system_admin)
        self.assertEqual(
            self.client.get(reverse("interactions:list")).context["rows"],
            [],
        )
        self.assertEqual(
            self.client.get(
                reverse("interactions:detail", args=(interaction.pk,))
            ).status_code,
            403,
        )

    def test_staff_can_only_access_assigned_interaction_records(self):
        assigned = self.create_interaction()
        assigned.incubatees.add(self.incubatee)
        assigned.assigned_users.add(self.staff)
        unassigned = self.create_interaction()

        self.login_as(self.staff)
        list_response = self.client.get(reverse("interactions:list"))
        visible_ids = {
            row["interaction"].pk for row in list_response.context["rows"]
        }
        self.assertIn(assigned.pk, visible_ids)
        self.assertNotIn(unassigned.pk, visible_ids)
        update_response = self.client.get(
            reverse("interactions:update", args=(assigned.pk,))
        )
        self.assertEqual(update_response.status_code, 200)
        denied_response = self.client.get(
            reverse("interactions:detail", args=(unassigned.pk,))
        )
        self.assertEqual(denied_response.status_code, 403)

    def test_board_is_read_only_and_freelancer_sees_own_interactions(self):
        admin_record = self.create_interaction()
        board_record = self.create_interaction()
        self.login_as(self.board)
        board_list = self.client.get(reverse("interactions:list"))
        self.assertIn(
            admin_record.pk,
            {row["interaction"].pk for row in board_list.context["rows"]},
        )
        self.assertNotContains(board_list, reverse("interactions:create"))
        self.assertEqual(
            self.client.get(
                reverse("interactions:update", args=(admin_record.pk,))
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("interactions:detail", args=(admin_record.pk,))
            ).status_code,
            200,
        )

        self.login_as(self.freelancer)
        create_response = self.client.post(
            reverse("interactions:create"),
            {
                "occurred_at": "2026-10-20T10:30",
                "duration_minutes": "0",
                "interaction_type": Interaction.Type.CALL,
                "topics": "Synthetic own call",
                "outcome": "",
                "sensitivity": Interaction.Sensitivity.INTERNAL,
            },
        )
        own_record = Interaction.objects.get(created_by=self.freelancer)
        freelancer_list = self.client.get(reverse("interactions:list"))
        self.assertEqual(create_response.status_code, 302)
        visible_ids = {
            row["interaction"].pk for row in freelancer_list.context["rows"]
        }
        self.assertIn(own_record.pk, visible_ids)
        self.assertNotIn(board_record.pk, visible_ids)
        self.assertEqual(
            self.client.get(
                reverse("interactions:detail", args=(admin_record.pk,))
            ).status_code,
            403,
        )

    def test_follow_up_tasks_and_event_attendance_are_authorized(self):
        interaction = self.create_interaction()
        interaction.incubatees.add(self.incubatee)
        interaction.participants.add(self.stakeholder)
        interaction.assigned_users.add(self.staff)
        task_response = self.client.post(
            reverse("interactions:task_add", args=(interaction.pk,)),
            {
                "description": "Synthetic follow-up task",
                "owner": self.staff.pk,
                "due_date": "2026-11-10",
                "status": FollowUpTask.Status.OPEN,
            },
        )
        self.assertEqual(task_response.status_code, 302)
        task = FollowUpTask.objects.get()
        self.assertEqual(task.owner, self.staff)
        self.assertEqual(task.due_date.isoformat(), "2026-11-10")
        self.assertEqual(
            self.client.get(
                reverse("interactions:detail", args=(interaction.pk,))
            ).status_code,
            200,
        )

        event = self.create_interaction(interaction_type=Interaction.Type.EVENT)
        event.participants.add(self.stakeholder)
        attendance_response = self.client.post(
            reverse("interactions:attendance_add", args=(event.pk,)),
            {
                "stakeholder": "",
                "participant_name": "Synthetic Walk-in Participant",
                "status": EventAttendance.Status.ATTENDED,
            },
        )
        self.assertEqual(attendance_response.status_code, 302)
        attendance = EventAttendance.objects.get()
        self.assertEqual(attendance.status, EventAttendance.Status.ATTENDED)
        self.assertEqual(attendance.display_name, "Synthetic Walk-in Participant")
        self.assertEqual(
            self.client.get(reverse("interactions:detail", args=(event.pk,))).status_code,
            200,
        )

    def test_restricted_interaction_is_not_visible_to_staff(self):
        interaction = self.create_interaction(
            sensitivity=Interaction.Sensitivity.RESTRICTED
        )
        interaction.assigned_users.add(self.staff)
        self.login_as(self.staff)
        response = self.client.get(
            reverse("interactions:detail", args=(interaction.pk,))
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_is_soft_and_history_is_retained(self):
        interaction = self.create_interaction()
        response = self.client.post(
            reverse("interactions:delete", args=(interaction.pk,))
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Interaction.objects.filter(pk=interaction.pk).exists())
        retained = Interaction.all_objects.get(pk=interaction.pk)
        self.assertTrue(retained.is_deleted)
        self.assertEqual(
            retained.history.first().change_type,
            InteractionHistory.ChangeType.DELETED,
        )
