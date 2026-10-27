from datetime import timedelta
from urllib.parse import urlsplit
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from accounts.access import accessible_queryset, has_access
from accounts.models import Role, RoleGrant
from audit.models import AuditEvent
from incubation.account_access import assignable_cferi_staff, manageable_account_businesses
from incubation.account_forms import IncubateeAccountForm
from incubation.forms import AssignmentForm
from incubation.models import (
    CheckInAccessLink,
    Incubatee,
    IncubateeAccount,
    IncubateeAccountBusiness,
    IncubateeBaselineSnapshot,
    IncubateeMetricObservation,
    IncubateeMilestone,
    IncubateeStatusHistory,
    MentoringSession,
    MonthlyCheckIn,
    Programme,
)

User = get_user_model()


class IncubateeAccountManagementTests(TestCase):
    def setUp(self):
        self.admin = self.create_user("admin@gmail.com", Role.Code.ADMIN)
        self.staff = self.create_user("staff@gmail.com", Role.Code.CFERI_STAFF)
        self.business = self.create_business("Synthetic Linked Business")
        self.other_business = self.create_business("Synthetic Unassigned Business")
        self.business.assigned_users.add(self.staff)

    def create_user(self, email, role_code):
        user = User.objects.create_user(
            email=email,
            password="Synthetic-Account-Test-91!",
            first_name="Test",
            last_name="Operator",
            access_expires_at=(
                timezone.now() + timedelta(days=2)
                if role_code in {Role.Code.FREELANCER, Role.Code.EXTERNAL_GUEST}
                else None
            ),
        )
        user.roles.add(Role.objects.get(code=role_code))
        return user

    def create_business(self, name, *, created_by=None):
        actor = created_by or self.admin
        return Incubatee.objects.create(
            legal_name=name,
            created_by=actor,
            updated_by=actor,
            owner=actor,
        )

    def login(self, user):
        self.client.force_login(user)

    def create_account(self, actor, email, business_ids=None, status="active"):
        self.login(actor)
        return self.client.post(
            reverse("incubation:account_create"),
            {
                "full_name": "Avery Synthetic",
                "email": email,
                "registration_mode": "existing_business",
                "businesses": business_ids or [str(self.business.pk)],
                "entrepreneur_category": IncubateeAccount.EntrepreneurCategory.STUDENT_ENTREPRENEUR,
                "account_status": status,
            },
        )

    def test_migration_preserves_existing_roles_and_adds_unprivileged_incubatee_role(self):
        expected = {
            "super_admin",
            "system_admin",
            "admin",
            "board_member",
            "cferi_staff",
            "freelancer",
            "external_guest",
            "incubatee",
        }
        self.assertEqual(set(Role.objects.values_list("code", flat=True)), expected)
        incubatee_role = Role.objects.get(code=Role.Code.INCUBATEE)
        self.assertFalse(incubatee_role.grants.exists())

    def test_all_active_non_expired_cferi_staff_are_assignable_until_admin_restricts_access(self):
        additional_staff = self.create_user(
            "additional.staff@gmail.com", Role.Code.CFERI_STAFF
        )
        expired_staff = self.create_user(
            "expired.staff@gmail.com", Role.Code.CFERI_STAFF
        )
        expired_staff.access_expires_at = timezone.now() - timedelta(minutes=1)
        expired_staff.save(update_fields=("access_expires_at",))
        restricted_staff = self.create_user(
            "restricted.staff@gmail.com", Role.Code.CFERI_STAFF
        )
        restricted_staff.is_active = False
        restricted_staff.save(update_fields=("is_active",))
        admin_form = IncubateeAccountForm(
            business_queryset=manageable_account_businesses(self.admin),
            actor=self.admin,
        )
        assignment_form = AssignmentForm(instance=self.business, user=self.admin)

        expected = {self.staff.pk, additional_staff.pk}
        self.assertEqual(
            set(assignable_cferi_staff().values_list("pk", flat=True)),
            expected,
        )
        self.assertEqual(
            set(admin_form.fields["assigned_staff"].queryset.values_list("pk", flat=True)),
            expected,
        )
        self.assertEqual(
            set(assignment_form.fields["staff_owner"].queryset.values_list("pk", flat=True)),
            expected,
        )

    def test_staff_can_invite_account_linked_only_to_authorized_existing_business(self):
        response = self.create_account(
            self.staff,
            "student.entrepreneur@gmail.com",
            [str(self.business.pk)],
        )

        self.assertRedirects(response, reverse("incubation:account_list"))
        account = IncubateeAccount.objects.get(user__email="student.entrepreneur@gmail.com")
        self.assertEqual(account.created_by, self.staff)
        self.assertEqual(account.business_links.count(), 1)
        link = account.business_links.get()
        self.assertEqual(link.incubatee, self.business)
        self.assertEqual(link.linked_by, self.staff)
        self.assertTrue(account.user.roles.filter(code=Role.Code.INCUBATEE).exists())
        self.assertFalse(account.user.is_staff)
        self.assertFalse(account.user.is_superuser)
        self.assertEqual(account.invitation_status, "pending")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("password-reset/", mail.outbox[0].body)
        self.assertContains(
            self.client.get(reverse("incubation:account_list")),
            "Pending",
        )
        self.assertNotIn("Synthetic-Account-Test", mail.outbox[0].body)
        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.staff,
                object_id=str(account.pk),
                resource="incubation.incubatee_account",
            ).exists()
        )

    def test_staff_can_register_new_business_and_capture_onboarding_baseline(self):
        programme = Programme.objects.create(name="Synthetic Venture Programme")
        self.login(self.staff)
        response = self.client.post(
            reverse("incubation:account_create"),
            {
                "registration_mode": "new_business",
                "full_name": "Idea Founder",
                "email": "idea.founder@gmail.com",
                "entrepreneur_category": IncubateeAccount.EntrepreneurCategory.STUDENT_ENTREPRENEUR,
                "new_business_name": "Synthetic Idea Venture",
                "business_description": "A proposed local food-growing business.",
                "business_registration_status": Incubatee.RegistrationStatus.NOT_REGISTERED,
                "business_stage": Incubatee.BusinessStage.IDEA,
                "programme": str(programme.pk),
                "assigned_staff": str(self.staff.pk),
                "initial_support_needs": "Business planning and market validation",
                "account_status": "active",
            },
        )

        self.assertRedirects(response, reverse("incubation:account_list"))
        account = IncubateeAccount.objects.get(user__email="idea.founder@gmail.com")
        business = account.business_links.get().incubatee
        self.assertEqual(business.legal_name, "Synthetic Idea Venture")
        self.assertEqual(business.products_services, "A proposed local food-growing business.")
        self.assertEqual(
            business.business_registration_status,
            Incubatee.RegistrationStatus.NOT_REGISTERED,
        )
        self.assertEqual(business.business_stage, Incubatee.BusinessStage.IDEA)
        self.assertEqual(business.programme, programme)
        self.assertEqual(business.staff_owner, self.staff)
        self.assertIn(self.staff, business.assigned_users.all())
        self.assertEqual(
            business.top_support_needs,
            ["Business planning and market validation"],
        )
        self.assertIsNone(business.cipc_registration_number)
        self.assertIsNone(business.date_registered)
        self.assertIsNone(business.revenue_12_months_zar)
        self.assertIsNone(business.customer_count)
        self.assertIsNone(business.full_time_employees)
        self.assertIsNone(business.part_time_employees)
        self.assertFalse(business.documents.exists())
        self.assertFalse(business.business_documents.exists())
        self.assertEqual(
            business.baseline_history.get().snapshot["business_stage"],
            Incubatee.BusinessStage.IDEA,
        )
        self.assertEqual(business.baseline_history.get().founder_snapshot, [])
        self.assertEqual(business.created_by, self.staff)
        self.assertEqual(
            IncubateeStatusHistory.objects.get(incubatee=business).new_status,
            Incubatee.Status.APPLICANT,
        )
        self.assertEqual(account.invitation_status, "pending")
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.staff,
                object_id=str(account.pk),
                resource="incubation.incubatee_account",
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                actor=self.staff,
                object_id=str(business.pk),
                resource="incubation.incubatee",
            ).exists()
        )

    def test_idea_stage_registration_does_not_require_formal_business_details(self):
        self.login(self.staff)
        response = self.client.post(
            reverse("incubation:account_create"),
            {
                "registration_mode": "new_business",
                "full_name": "Student Founder",
                "email": "student.founder@gmail.com",
                "entrepreneur_category": IncubateeAccount.EntrepreneurCategory.STUDENT_ENTREPRENEUR,
                "new_business_name": "Unregistered Student Idea",
                "business_description": "An early-stage idea.",
                "business_registration_status": Incubatee.RegistrationStatus.NOT_REGISTERED,
                "business_stage": Incubatee.BusinessStage.IDEA,
                "assigned_staff": str(self.staff.pk),
                "account_status": "active",
            },
        )

        self.assertRedirects(response, reverse("incubation:account_list"))
        business = IncubateeAccount.objects.get(
            user__email="student.founder@gmail.com"
        ).business_links.get().incubatee
        self.assertFalse(business.cipc_registration_number)
        self.assertIsNone(business.date_registered)
        self.assertIsNone(business.monthly_average_revenue_zar)
        self.assertIsNone(business.customer_count)
        self.assertIsNone(business.full_time_employees)
        self.assertIsNone(business.part_time_employees)
        self.assertFalse(business.documents.exists())
        self.assertFalse(business.business_documents.exists())

    def test_new_business_registration_rejects_existing_business_name(self):
        self.login(self.staff)
        response = self.client.post(
            reverse("incubation:account_create"),
            {
                "registration_mode": "new_business",
                "full_name": "Duplicate Founder",
                "email": "duplicate.founder@gmail.com",
                "entrepreneur_category": IncubateeAccount.EntrepreneurCategory.EXTERNAL_SME,
                "new_business_name": self.business.legal_name.upper(),
                "business_description": "A duplicate should be linked instead.",
                "business_registration_status": Incubatee.RegistrationStatus.REGISTERED,
                "business_stage": Incubatee.BusinessStage.EARLY_REVENUE,
                "assigned_staff": str(self.staff.pk),
                "account_status": "active",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Search for and link the existing business")
        self.assertFalse(
            User.objects.filter(email="duplicate.founder@gmail.com").exists()
        )
        self.assertEqual(Incubatee.objects.filter(legal_name__icontains="Synthetic").count(), 2)
        self.assertFalse(mail.outbox)

    def test_new_registration_rolls_back_business_and_account_if_invitation_fails(self):
        self.login(self.staff)
        with patch(
            "incubation.account_views.UserPasswordResetForm.save",
            side_effect=RuntimeError("Invitation could not be sent."),
        ):
            with self.assertRaisesMessage(RuntimeError, "Invitation could not be sent."):
                self.client.post(
                    reverse("incubation:account_create"),
                    {
                        "registration_mode": "new_business",
                        "full_name": "Rollback Founder",
                        "email": "rollback.founder@gmail.com",
                        "entrepreneur_category": IncubateeAccount.EntrepreneurCategory.STUDENT_ENTREPRENEUR,
                        "new_business_name": "Rolled Back Venture",
                        "business_description": "Should be rolled back.",
                        "business_registration_status": Incubatee.RegistrationStatus.NOT_REGISTERED,
                        "business_stage": Incubatee.BusinessStage.IDEA,
                        "assigned_staff": str(self.staff.pk),
                        "account_status": "active",
                    },
                )

        self.assertFalse(User.objects.filter(email="rollback.founder@gmail.com").exists())
        self.assertFalse(IncubateeAccount.objects.filter(user__email="rollback.founder@gmail.com").exists())
        self.assertFalse(Incubatee.objects.filter(legal_name="Rolled Back Venture").exists())
        self.assertFalse(IncubateeBaselineSnapshot.objects.filter(
            incubatee__legal_name="Rolled Back Venture"
        ).exists())

    def test_staff_can_link_any_non_restricted_existing_business(self):
        Incubatee.objects.filter(pk=self.other_business.pk).update(
            created_at=timezone.now() - timedelta(days=400)
        )
        response = self.create_account(
            self.staff,
            "unassigned.entrepreneur@gmail.com",
            [str(self.other_business.pk)],
        )

        self.assertRedirects(response, reverse("incubation:account_list"))
        account = IncubateeAccount.objects.get(
            user__email="unassigned.entrepreneur@gmail.com"
        )
        self.assertEqual(account.business_links.get().incubatee, self.other_business)
        snapshot = IncubateeBaselineSnapshot.objects.get(
            incubatee=self.other_business
        )
        self.assertEqual(snapshot.source, "staff_existing_business_link")
        self.assertEqual(
            snapshot.snapshot["date_joined_cferi"][:10],
            account.business_links.get().linked_at.date().isoformat(),
        )
        self.assertEqual(account.created_by, self.staff)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("password-reset/", mail.outbox[0].body)

    def test_staff_account_profile_hides_activity_for_unassigned_business(self):
        account_response = self.create_account(
            self.staff,
            "out_of_scope.rep@gmail.com",
            [str(self.other_business.pk)],
        )
        self.assertEqual(account_response.status_code, 302)
        account = IncubateeAccount.objects.get(user__email="out_of_scope.rep@gmail.com")
        MonthlyCheckIn.objects.create(
            incubatee=self.other_business,
            period_start=timezone.localdate().replace(year=2020, month=1, day=1),
            submitted_at=timezone.now(),
        )
        self.login(self.staff)

        profile = self.client.get(
            reverse("incubation:account_profile", args=(account.pk,))
        )

        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, "Synthetic Unassigned Business")
        self.assertContains(profile, "Business details and activity are hidden")
        self.assertNotContains(profile, "January 2020")

    def test_staff_cannot_link_restricted_business_to_incubatee_account(self):
        self.other_business.sensitivity = Incubatee.Sensitivity.RESTRICTED
        self.other_business.save(update_fields=("sensitivity",))

        response = self.create_account(
            self.staff,
            "restricted.business.rep@gmail.com",
            [str(self.other_business.pk)],
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            User.objects.filter(email="restricted.business.rep@gmail.com").exists()
        )
        self.assertFalse(mail.outbox)

    def test_multiple_authorized_representatives_link_to_one_business_without_duplicates(self):
        first = self.create_account(self.admin, "first.rep@gmail.com")
        second = self.create_account(self.admin, "second.rep@gmail.com")

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Incubatee.objects.filter(pk=self.business.pk).count(), 1)
        self.assertEqual(
            IncubateeAccountBusiness.objects.filter(incubatee=self.business).count(),
            2,
        )

    def test_incubatee_only_reads_explicitly_linked_businesses_and_not_other_resources(self):
        response = self.create_account(self.admin, "business.rep@gmail.com")
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="business.rep@gmail.com")

        visible = accessible_queryset(
            user,
            RoleGrant.Resource.INCUBATEES,
            RoleGrant.Action.READ,
            Incubatee.objects.all(),
        )
        self.assertEqual(list(visible), [self.business])
        self.assertTrue(
            has_access(
                user,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.READ,
                self.business,
            )
        )
        self.assertFalse(
            has_access(
                user,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.READ,
                self.other_business,
            )
        )
        self.assertFalse(
            has_access(
                user,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.UPDATE,
                self.business,
            )
        )
        self.business.sensitivity = Incubatee.Sensitivity.CONFIDENTIAL
        self.assertFalse(
            has_access(
                user,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.READ,
                self.business,
            )
        )
        self.business.sensitivity = Incubatee.Sensitivity.INTERNAL
        self.assertFalse(
            has_access(
                user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
            )
        )
        self.assertFalse(
            has_access(user, RoleGrant.Resource.USERS, RoleGrant.Action.READ)
        )
        user.roles.add(Role.objects.get(code=Role.Code.ADMIN))
        self.assertFalse(
            has_access(user, RoleGrant.Resource.USERS, RoleGrant.Action.READ)
        )

    def test_incubatee_role_cannot_gain_other_access_from_role_grants(self):
        response = self.create_account(self.admin, "restricted.rep@gmail.com")
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="restricted.rep@gmail.com")
        role = Role.objects.get(code=Role.Code.INCUBATEE)
        RoleGrant.objects.create(
            role=role,
            resource=RoleGrant.Resource.USERS,
            action=RoleGrant.Action.READ,
            scope=RoleGrant.Scope.ALL,
        )

        self.assertFalse(
            has_access(user, RoleGrant.Resource.USERS, RoleGrant.Action.READ)
        )

    def test_incubatee_can_login_to_limited_dashboard_and_not_see_admin_navigation(self):
        response = self.create_account(self.admin, "dashboard.rep@gmail.com")
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="dashboard.rep@gmail.com")
        self.login(user)

        dashboard = self.client.get(reverse("reporting:dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Incubatee workspace")
        self.assertContains(dashboard, self.business.legal_name)
        self.assertNotContains(dashboard, "Centre documents")
        self.assertNotContains(dashboard, "Manage accounts")

    def create_incubatee_account(self, email, business=None):
        user = self.create_user(email, Role.Code.INCUBATEE)
        account = IncubateeAccount.objects.create(
            user=user,
            entrepreneur_category=IncubateeAccount.EntrepreneurCategory.STUDENT_ENTREPRENEUR,
            created_by=self.admin,
        )
        IncubateeAccountBusiness.objects.create(
            account=account,
            incubatee=business or self.business,
            linked_by=self.admin,
        )
        return user, account

    def test_incubatee_dashboard_keeps_performance_in_reports_not_dashboard(self):
        user, _account = self.create_incubatee_account("metrics.rep@gmail.com")
        current_period = timezone.localdate().replace(day=1)
        MonthlyCheckIn.objects.create(
            incubatee=self.business,
            period_start=current_period,
            sent_at=timezone.now(),
            submitted_at=timezone.now(),
            completeness_score=80,
        )
        previous_period = current_period - timedelta(days=1)
        outstanding_checkin = MonthlyCheckIn.objects.create(
            incubatee=self.business,
            period_start=previous_period.replace(day=1),
            due_date=timezone.localdate() + timedelta(days=1),
            sent_at=timezone.now(),
        )
        CheckInAccessLink.objects.create(
            checkin=outstanding_checkin,
            recipient_email=user.email,
            token_digest="a" * 64,
            expires_at=timezone.now() + timedelta(days=2),
            email_sent_at=timezone.now(),
        )
        IncubateeMilestone.objects.create(
            incubatee=self.business,
            milestone_type=IncubateeMilestone.Type.FIRST_SALE,
            milestone_date=timezone.localdate(),
            recorded_by=self.admin,
        )
        MentoringSession.objects.create(
            incubatee=self.business,
            session_date=timezone.localdate() + timedelta(days=2),
            mentoring_hours="1.00",
            recorded_by=self.admin,
        )
        for metric, value in (
            (IncubateeMetricObservation.Metric.REVENUE_ZAR, {"decimal_value": "1250.00"}),
            (IncubateeMetricObservation.Metric.CUSTOMER_COUNT, {"integer_value": 17}),
            (IncubateeMetricObservation.Metric.FULL_TIME_EMPLOYEES, {"integer_value": 2}),
            (IncubateeMetricObservation.Metric.PART_TIME_EMPLOYEES, {"integer_value": 3}),
            (IncubateeMetricObservation.Metric.FUNDING_RECEIVED_ZAR, {"decimal_value": "5000.00"}),
        ):
            IncubateeMetricObservation.objects.create(
                incubatee=self.business,
                period_type=IncubateeMetricObservation.PeriodType.MONTHLY,
                period_start=timezone.localdate().replace(day=1),
                metric=metric,
                source="test",
                **value,
            )
        self.login(user)

        response = self.client.get(reverse("incubation:incubatee_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Latest monthly revenue")
        self.assertNotContains(response, "Latest customer count")
        self.assertNotContains(response, "Latest employment count")
        self.assertNotContains(response, "Latest funding received this period")
        self.assertNotContains(response, "Progress update")
        self.assertContains(response, self.business.legal_name)
        self.assertContains(response, "Applicant")
        for dashboard_detail in (
            "Outstanding submissions",
            "Monthly progress link sent",
            "First sale",
            "Upcoming Mentorship",
            "Mentorship session",
            "No recent activity recorded",
            "No recent notifications",
            "No support need recorded",
        ):
            self.assertNotContains(response, dashboard_detail)
        for route_name, label in (
            ("business_profile", "My Business Profile"),
            ("business_document_list", "Business Documents"),
            ("progress_history", "Monthly Progress"),
            ("milestone_list", "Business Milestones"),
            ("challenge_list", "Challenges"),
            ("support_request_list", "Support Requests"),
            ("mentorship_portal", "My Mentorship"),
            ("notification_centre", "Notifications"),
        ):
            self.assertContains(
                response,
                f'href="{reverse(f"incubation:{route_name}", args=(self.business.pk,))}">{label}</a>',
            )
        self.assertContains(
            response,
            f'href="{reverse("incubation:incubatee_reports", args=(self.business.pk,))}">Business Performance &amp; Reports</a>',
        )

        reports = self.client.get(
            reverse("incubation:incubatee_reports", args=(self.business.pk,))
        )
        self.assertEqual(reports.status_code, 200)
        self.assertContains(reports, "Business performance trends")

    def test_incubatee_can_select_only_an_explicitly_linked_business(self):
        user, account = self.create_incubatee_account("selector.rep@gmail.com")
        linked_business = self.create_business("Second Linked Business")
        IncubateeAccountBusiness.objects.create(
            account=account,
            incubatee=linked_business,
            linked_by=self.admin,
        )
        self.login(user)

        selected = self.client.get(
            reverse("incubation:incubatee_dashboard"),
            {"business": linked_business.pk},
        )
        tampered = self.client.get(
            reverse("incubation:incubatee_dashboard"),
            {"business": self.other_business.pk},
        )
        direct_detail = self.client.get(
            reverse("incubation:detail", args=(self.other_business.pk,))
        )

        self.assertEqual(selected.status_code, 200)
        self.assertContains(selected, "Second Linked Business")
        self.assertEqual(tampered.status_code, 404)
        self.assertIn(direct_detail.status_code, (403, 404))

    def test_incubatee_cannot_view_confidential_business_or_change_lifecycle_status(self):
        user, _account = self.create_incubatee_account("confidential.rep@gmail.com")
        self.business.sensitivity = Incubatee.Sensitivity.CONFIDENTIAL
        self.business.save()
        self.login(user)

        dashboard = self.client.get(reverse("incubation:incubatee_dashboard"))
        status_change = self.client.post(
            reverse("incubation:status", args=(self.business.pk,)),
            {"status": Incubatee.Status.GRADUATED},
        )

        self.assertContains(dashboard, "No business is currently available")
        self.assertIn(status_change.status_code, (403, 404))

    def test_incubatee_login_redirects_to_its_dashboard_even_with_other_next_url(self):
        user = User.objects.create_user(
            email="login.incubatee@gmail.com",
            password="Dashboard-Login-Test-2026!",
            first_name="Login",
            last_name="Incubatee",
        )
        user.roles.add(Role.objects.get(code=Role.Code.INCUBATEE))
        IncubateeAccount.objects.create(
            user=user,
            entrepreneur_category=IncubateeAccount.EntrepreneurCategory.EXTERNAL_SME,
            created_by=self.admin,
        )

        response = self.client.post(
            reverse("accounts:login"),
            {
                "username": user.email,
                "password": "Dashboard-Login-Test-2026!",
                "next": reverse("reporting:dashboard"),
            },
        )

        self.assertRedirects(response, reverse("incubation:incubatee_dashboard"))

    def test_cferi_staff_dashboard_has_manage_incubatees_section(self):
        self.login(self.staff)

        dashboard = self.client.get(reverse("reporting:cferi_staff"))

        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(
            dashboard, "Register incubatee accounts, link existing businesses"
        )
        self.assertNotContains(dashboard, "Monitoring data is limited")
        self.assertNotContains(
            dashboard,
            reverse("stakeholders:list"),
            msg_prefix="The CFERI Staff navigation should not link to stakeholders.",
        )
        self.assertContains(dashboard, reverse("incubation:account_list"))
        self.assertContains(
            dashboard, reverse("incubation:staff_incubatee_monitoring")
        )
        self.assertContains(
            dashboard, reverse("incubation:staff_outstanding_progress")
        )
        self.assertContains(dashboard, reverse("incubation:staff_recent_progress"))
        monitoring = self.client.get(
            reverse("incubation:staff_incubatee_monitoring")
        )
        outstanding = self.client.get(
            reverse("incubation:staff_outstanding_progress")
        )
        recent = self.client.get(reverse("incubation:staff_recent_progress"))
        self.assertEqual(monitoring.status_code, 200)
        self.assertEqual(outstanding.status_code, 200)
        self.assertEqual(recent.status_code, 200)
        self.login(self.admin)
        self.assertEqual(
            self.client.get(
                reverse("incubation:staff_incubatee_monitoring")
            ).status_code,
            403,
        )

    def test_account_manager_can_search_and_suspend_or_reactivate_account(self):
        response = self.create_account(self.staff, "status.rep@gmail.com")
        self.assertEqual(response.status_code, 302)
        account = IncubateeAccount.objects.get(user__email="status.rep@gmail.com")
        self.login(self.staff)

        search = self.client.get(
            reverse("incubation:account_list"),
            {"q": "Synthetic Linked Business"},
        )
        self.assertContains(search, "status.rep@gmail.com")

        suspended = self.client.post(
            reverse("incubation:account_status", args=(account.pk,)),
            {"status": "suspended"},
        )
        self.assertRedirects(suspended, reverse("incubation:account_list"))
        account.user.refresh_from_db()
        self.assertFalse(account.user.is_active)

        reactivated = self.client.post(
            reverse("incubation:account_status", args=(account.pk,)),
            {"status": "active"},
        )
        self.assertRedirects(reactivated, reverse("incubation:account_list"))
        account.user.refresh_from_db()
        self.assertTrue(account.user.is_active)

    def test_staff_can_manage_cross_business_account_without_accessing_unassigned_activity(self):
        response = self.create_account(
            self.admin,
            "cross-business.rep@gmail.com",
            [str(self.business.pk), str(self.other_business.pk)],
        )
        self.assertEqual(response.status_code, 302)
        account = IncubateeAccount.objects.get(
            user__email="cross-business.rep@gmail.com"
        )
        self.login(self.staff)

        listing = self.client.get(reverse("incubation:account_list"))
        self.assertEqual(len(listing.context["account_rows"]), 1)
        self.assertEqual(
            {
                business.pk
                for business in listing.context["account_rows"][0]["businesses"]
            },
            {self.business.pk, self.other_business.pk},
        )
        MonthlyCheckIn.objects.create(
            incubatee=self.other_business,
            period_start=timezone.localdate().replace(year=2020, month=1, day=1),
            submitted_at=timezone.now(),
        )
        profile = self.client.get(
            reverse("incubation:account_profile", args=(account.pk,))
        )
        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, "Synthetic Unassigned Business")
        self.assertContains(profile, "Business details and activity are hidden")
        self.assertNotContains(profile, "January 2020")
        response = self.client.post(
            reverse("incubation:account_status", args=(account.pk,)),
            {"status": "suspended"},
        )

        self.assertRedirects(response, reverse("incubation:account_list"))
        account.user.refresh_from_db()
        self.assertFalse(account.user.is_active)

    @override_settings(PASSWORD_RESET_TIMEOUT=60)
    def test_expired_invitation_can_be_resent_and_status_is_updated(self):
        response = self.create_account(self.staff, "resend.rep@gmail.com")
        self.assertEqual(response.status_code, 302)
        account = IncubateeAccount.objects.get(user__email="resend.rep@gmail.com")
        account.invitation_sent_at = timezone.now() - timedelta(minutes=2)
        account.save(update_fields=("invitation_sent_at",))
        self.assertEqual(account.invitation_status, "expired")
        mail.outbox.clear()
        self.login(self.staff)

        resent = self.client.post(
            reverse("incubation:account_resend_invitation", args=(account.pk,))
        )

        self.assertRedirects(resent, reverse("incubation:account_list"))
        account.refresh_from_db()
        self.assertEqual(account.invitation_status, "pending")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("password-reset/", mail.outbox[0].body)

    def test_activation_link_sets_password_and_marks_invitation_accepted(self):
        response = self.create_account(self.admin, "activate.rep@gmail.com")
        self.assertEqual(response.status_code, 302)
        account = IncubateeAccount.objects.get(user__email="activate.rep@gmail.com")
        activation_url = next(
            part for part in mail.outbox[0].body.split() if "password-reset/" in part
        )
        confirm = self.client.get(urlsplit(activation_url).path, follow=True)
        self.assertEqual(confirm.status_code, 200)
        password = "Unique-Activation-Password-2026!"
        completed = self.client.post(
            confirm.request["PATH_INFO"],
            {"new_password1": password, "new_password2": password},
        )

        self.assertEqual(completed.status_code, 302)
        account.refresh_from_db()
        self.assertEqual(account.invitation_status, "accepted")
        self.assertEqual(account.account_status, "Active")
        self.assertTrue(account.user.check_password(password))

    def test_only_admin_and_cferi_staff_can_manage_incubatee_accounts(self):
        outsider = self.create_user("freelancer@gmail.com", Role.Code.FREELANCER)
        for route in (
            reverse("incubation:account_list"),
            reverse("incubation:account_create"),
        ):
            with self.subTest(route=route):
                self.login(outsider)
                self.assertEqual(self.client.get(route).status_code, 403)
                if route == reverse("incubation:account_create"):
                    self.assertEqual(
                        self.client.post(
                            route,
                            {
                                "registration_mode": "new_business",
                                "full_name": "Unauthorized Founder",
                                "email": "unauthorized.founder@gmail.com",
                            },
                        ).status_code,
                        403,
                    )

        account_user = self.create_user("incubatee@gmail.com", Role.Code.INCUBATEE)
        self.login(account_user)
        self.assertEqual(
            self.client.get(reverse("incubation:account_list")).status_code,
            403,
        )

    def test_staff_without_assignments_can_register_and_invite_for_existing_businesses(self):
        self.business.assigned_users.remove(self.staff)
        self.login(self.staff)

        account_list = self.client.get(reverse("incubation:account_list"))
        create_form = self.client.get(reverse("incubation:account_create"))

        self.assertEqual(account_list.status_code, 200)
        self.assertContains(account_list, "Add incubatee")
        self.assertEqual(create_form.status_code, 200)
        self.assertContains(create_form, "Synthetic Linked Business")
        self.assertContains(create_form, "Synthetic Unassigned Business")

    def test_existing_business_search_limits_link_choices(self):
        self.login(self.staff)

        response = self.client.get(
            reverse("incubation:account_create"),
            {"business_q": "Unassigned"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synthetic Unassigned Business")
        self.assertNotContains(response, "Synthetic Linked Business</option>")

    def test_incubatee_dashboard_and_management_are_not_public(self):
        self.assertEqual(
            self.client.get(reverse("incubation:account_list")).status_code,
            403,
        )
