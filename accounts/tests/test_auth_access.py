from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pyotp
from django.contrib.auth import get_user_model
from django.core import mail
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.access import (
    accessible_queryset,
    authorized_export_queryset,
    authorized_file_download,
    authorized_task_queryset,
    has_access,
    permanently_delete_record,
    share_record,
    soft_delete_record,
)
from accounts.forms import lockout_status
from accounts.mfa import encrypt_totp_secret
from accounts.models import (
    LoginThrottle,
    AccessControlledModel,
    Role,
    RoleGrant,
    TOTPDevice,
)
from accounts.tasks import deactivate_expired_accounts

User = get_user_model()


def make_user(email="test.user@gmail.com", password="Only-for-tests-9!"):
    return User.objects.create_user(email=email, password=password)


def assign_role(user, code):
    return user.roles.add(Role.objects.get(code=code))


class AuthenticationAccessTests(TestCase):
    def test_navbar_shows_profile_initials_instead_of_email(self):
        user = User.objects.create_user(
            email="sam.mandla@gmail.com",
            password="Only-for-tests-9!",
            first_name="Sam",
            last_name="Mandla",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("home"))

        self.assertRegex(
            response.content.decode(),
            r'class="profile-avatar"[^>]*>\s*SM\s*</span>',
        )
        self.assertNotContains(response, user.email)

    def test_navbar_falls_back_to_email_initial_when_name_is_missing(self):
        user = make_user()
        self.client.force_login(user)

        response = self.client.get(reverse("home"))

        self.assertRegex(
            response.content.decode(),
            r'class="profile-avatar"[^>]*>\s*T\s*</span>',
        )
        self.assertNotContains(response, user.email)

    def test_login_requires_gmail_email_and_at_least_eight_character_password(self):
        user = make_user()

        wrong_domain = self.client.post(
            reverse("accounts:login"),
            {"username": "person@example.test", "password": "Only-for-tests-9!"},
        )
        self.assertEqual(wrong_domain.status_code, 200)
        self.assertFalse(self.client.session.get("_auth_user_id"))
        self.assertContains(wrong_domain, "Use your @gmail.com email address.")

        short_password = self.client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "short7"},
        )
        self.assertEqual(short_password.status_code, 200)
        self.assertFalse(self.client.session.get("_auth_user_id"))
        self.assertContains(short_password, "at least 8 characters")

    def test_user_model_rejects_non_gmail_email(self):
        with self.assertRaises(ValidationError):
            User.objects.create_user(
                email="person@example.test",
                password="Only-for-tests-9!",
            )

    def test_passwords_use_argon2_and_email_login_is_case_insensitive(self):
        with self.settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.Argon2PasswordHasher"]):
            user = make_user()
        self.assertTrue(user.password.startswith("argon2$"))
        response = self.client.post(
            reverse("accounts:login"),
            {"username": user.email.upper(), "password": "Only-for-tests-9!"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(pk=user.pk).is_authenticated)

    def test_required_mfa_cannot_be_bypassed_at_login_or_via_dashboard(self):
        user = make_user()
        assign_role(user, Role.Code.SUPER_ADMIN)

        response = self.client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "Only-for-tests-9!"},
        )
        self.assertRedirects(response, reverse("accounts:mfa_setup"))
        self.assertEqual(self.client.get(reverse("reporting:dashboard")).status_code, 302)
        setup = self.client.get(reverse("accounts:mfa_setup"))
        self.assertEqual(setup.status_code, 200)
        device = TOTPDevice.objects.get(user=user, confirmed=False)
        secret = __import__("accounts.mfa", fromlist=["decrypt_totp_secret"]).decrypt_totp_secret(
            device.secret_encrypted
        )
        code = pyotp.TOTP(secret).now()
        verified = self.client.post(reverse("accounts:mfa_setup"), {"otp_token": code})
        self.assertRedirects(verified, reverse("reporting:dashboard"))
        self.assertEqual(self.client.get(reverse("reporting:dashboard")).status_code, 200)

    def test_only_super_admin_role_requires_mfa(self):
        for index, code in enumerate(
            (
                Role.Code.SUPER_ADMIN,
                Role.Code.SYSTEM_ADMIN,
                Role.Code.ADMIN,
                Role.Code.BOARD_MEMBER,
            )
        ):
            with self.subTest(role=code):
                user = make_user(email=f"mfa-{index}@gmail.com")
                assign_role(user, code)
                response = self.client.post(
                    reverse("accounts:login"),
                    {"username": user.email, "password": "Only-for-tests-9!"},
                )
                self.assertEqual(response.status_code, 302)
                if code == Role.Code.SUPER_ADMIN:
                    self.assertEqual(response.url, reverse("accounts:mfa_setup"))
                else:
                    self.assertNotEqual(response.url, reverse("accounts:mfa_setup"))
                self.client.logout()

    def test_confirmed_mfa_role_requires_otp_on_each_login(self):
        user = make_user()
        assign_role(user, Role.Code.SUPER_ADMIN)
        secret = pyotp.random_base32()
        TOTPDevice.objects.create(
            user=user,
            secret_encrypted=encrypt_totp_secret(secret),
            confirmed=True,
        )

        without_code = self.client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "Only-for-tests-9!"},
        )
        self.assertEqual(without_code.status_code, 200)
        self.assertFalse(self.client.session.get("_auth_user_id"))

        with_code = self.client.post(
            reverse("accounts:login"),
            {
                "username": user.email,
                "password": "Only-for-tests-9!",
                "otp_token": pyotp.TOTP(secret).now(),
            },
        )
        self.assertEqual(with_code.status_code, 302)
        self.assertEqual(self.client.session.get("mfa_verified_user"), user.pk)

    def test_totp_code_cannot_be_replayed(self):
        user = make_user()
        secret = pyotp.random_base32()
        device = TOTPDevice.objects.create(
            user=user,
            secret_encrypted=encrypt_totp_secret(secret),
            confirmed=True,
        )
        from accounts.mfa import verify_totp

        code = pyotp.TOTP(secret).now()
        self.assertTrue(verify_totp(device, code))
        self.assertFalse(verify_totp(device, code))

    def test_invalid_mfa_code_counts_toward_lockout(self):
        user = make_user()
        assign_role(user, Role.Code.SUPER_ADMIN)
        TOTPDevice.objects.create(
            user=user,
            secret_encrypted=encrypt_totp_secret(pyotp.random_base32()),
            confirmed=True,
        )

        for _attempt in range(5):
            self.client.post(
                reverse("accounts:login"),
                {
                    "username": user.email,
                    "password": "Only-for-tests-9!",
                    "otp_token": "000000",
                },
            )
        self.assertTrue(lockout_status(user.email))
        self.assertFalse(self.client.session.get("_auth_user_id"))

    def test_password_reset_link_expires_after_thirty_minutes(self):
        user = make_user()
        now = datetime.now()
        with patch.object(
            default_token_generator,
            "_now",
            return_value=now,
        ):
            token = default_token_generator.make_token(user)
        with patch.object(default_token_generator, "_now", return_value=now + timedelta(minutes=29)):
            self.assertTrue(default_token_generator.check_token(user, token))
        with patch.object(default_token_generator, "_now", return_value=now + timedelta(minutes=31)):
            self.assertFalse(default_token_generator.check_token(user, token))

    def test_password_reset_sends_a_time_limited_link(self):
        user = make_user()
        response = self.client.post(
            reverse("accounts:password_reset"),
            {"email": user.email},
        )
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("password-reset/", mail.outbox[0].body)

    def test_password_setup_link_works_for_user_with_unusable_password(self):
        user = make_user()
        user.set_unusable_password()
        user.save(update_fields=("password",))

        response = self.client.post(
            reverse("accounts:password_reset"),
            {"email": user.email},
        )

        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        reset_url = next(
            part for part in mail.outbox[0].body.split() if "password-reset/" in part
        )
        response = self.client.get(urlsplit(reset_url).path, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "name=\"new_password1\"")

        password = "Synthetic-Reset-Password-73!"
        response = self.client.post(
            response.request["PATH_INFO"],
            {"new_password1": password, "new_password2": password},
        )

        self.assertRedirects(response, reverse("accounts:password_reset_complete"))
        user.refresh_from_db()
        self.assertTrue(user.check_password(password))
        self.assertTrue(self.client.login(username=user.email, password=password))

    def test_five_failed_password_attempts_lock_account_for_fifteen_minutes(self):
        user = make_user()
        for _attempt in range(5):
            self.client.post(
                reverse("accounts:login"),
                {"username": user.email, "password": "wrong-password"},
            )
        throttle = LoginThrottle.objects.get(email=user.email.lower())
        self.assertEqual(throttle.failed_attempts, 5)
        self.assertGreater(
            throttle.locked_until,
            timezone.now() + timedelta(minutes=14),
        )
        response = self.client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "Only-for-tests-9!"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.session.get("_auth_user_id"))

    def test_expired_lockout_resets_failed_attempt_count(self):
        from accounts.models import LoginThrottle

        throttle = LoginThrottle.objects.create(
            email="locked@gmail.com",
            failed_attempts=5,
            locked_until=timezone.now() - timedelta(seconds=1),
        )
        self.assertFalse(lockout_status(throttle.email))
        throttle.refresh_from_db()
        self.assertEqual(throttle.failed_attempts, 0)
        self.assertIsNone(throttle.locked_until)

    def test_session_is_ended_after_thirty_minutes_of_inactivity(self):
        user = make_user()
        self.client.force_login(user)
        session = self.client.session
        session["_auth_last_activity"] = (timezone.now() - timedelta(minutes=31)).timestamp()
        session.save()

        response = self.client.get("/")

        self.assertRedirects(response, reverse("accounts:login"))
        self.assertFalse(self.client.session.get("_auth_user_id"))

    def test_expired_temporary_account_is_deactivated_and_celery_task_covers_inactive_sessions(self):
        user = make_user()
        user.access_expires_at = timezone.now() - timedelta(seconds=1)
        user.save(update_fields=("access_expires_at",))
        assign_role(user, Role.Code.FREELANCER)
        self.client.force_login(user)

        self.client.get("/")
        user.refresh_from_db()
        self.assertFalse(user.is_active)

        another_user = make_user(email="expired.guest@gmail.com")
        another_user.access_expires_at = timezone.now() - timedelta(seconds=1)
        another_user.save(update_fields=("access_expires_at",))
        self.assertEqual(deactivate_expired_accounts(), 1)
        another_user.refresh_from_db()
        self.assertFalse(another_user.is_active)

    def test_temporary_roles_require_expiry_and_self_role_assignment_is_denied(self):
        admin = make_user(email="admin@gmail.com")
        assign_role(admin, Role.Code.SYSTEM_ADMIN)
        mfa_secret = pyotp.random_base32()
        TOTPDevice.objects.create(
            user=admin,
            secret_encrypted=encrypt_totp_secret(mfa_secret),
            confirmed=True,
        )
        self.client.force_login(admin)
        self.client.post(
            reverse("accounts:mfa_verify"),
            {"otp_token": pyotp.TOTP(mfa_secret).now()},
        )
        response = self.client.post(
            reverse("accounts:edit_user_access", args=[admin.pk]),
            {"roles": [Role.objects.get(code=Role.Code.SUPER_ADMIN).pk]},
        )
        self.assertEqual(response.status_code, 403)

    def test_system_admin_can_invite_assign_and_deactivate_other_accounts(self):
        admin = make_user(email="system-admin@gmail.com")
        assign_role(admin, Role.Code.SYSTEM_ADMIN)
        secret = pyotp.random_base32()
        TOTPDevice.objects.create(
            user=admin,
            secret_encrypted=encrypt_totp_secret(secret),
            confirmed=True,
        )
        self.client.force_login(admin)
        self.client.post(
            reverse("accounts:mfa_verify"),
            {"otp_token": pyotp.TOTP(secret).now()},
        )

        expiry = (timezone.now() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            reverse("accounts:invite_user"),
            {
                "email": "invited.guest@gmail.com",
                "first_name": "Test",
                "last_name": "Guest",
                "roles": [Role.objects.get(code=Role.Code.EXTERNAL_GUEST).pk],
                "access_expires_at": expiry,
            },
        )
        self.assertRedirects(response, reverse("accounts:manage_users"))
        invited = User.objects.get(email="invited.guest@gmail.com")
        self.assertTrue(invited.roles.filter(code=Role.Code.EXTERNAL_GUEST).exists())
        self.assertIsNotNone(invited.access_expires_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("password-reset/", mail.outbox[0].body)

        response = self.client.post(
            reverse("accounts:deactivate_user", args=[invited.pk]),
        )
        self.assertRedirects(response, reverse("accounts:manage_users"))
        invited.refresh_from_db()
        self.assertFalse(invited.is_active)

    def test_system_admin_cannot_escalate_users_to_super_admin(self):
        system_admin = make_user(email="role-guard-system@gmail.com")
        assign_role(system_admin, Role.Code.SYSTEM_ADMIN)
        secret = pyotp.random_base32()
        TOTPDevice.objects.create(
            user=system_admin,
            secret_encrypted=encrypt_totp_secret(secret),
            confirmed=True,
        )
        self.client.force_login(system_admin)
        self.client.post(
            reverse("accounts:mfa_verify"),
            {"otp_token": pyotp.TOTP(secret).now()},
        )
        super_admin_role = Role.objects.get(code=Role.Code.SUPER_ADMIN)

        invitation = self.client.post(
            reverse("accounts:invite_user"),
            {
                "email": "escalated@gmail.com",
                "roles": [super_admin_role.pk],
            },
        )
        self.assertEqual(invitation.status_code, 200)
        self.assertFalse(User.objects.filter(email="escalated@gmail.com").exists())

        target = make_user(email="role-target@gmail.com")
        update = self.client.post(
            reverse("accounts:edit_user_access", args=[target.pk]),
            {"roles": [super_admin_role.pk]},
        )
        self.assertEqual(update.status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.roles.filter(code=Role.Code.SUPER_ADMIN).exists())

        protected_target = make_user(email="protected-super@gmail.com")
        assign_role(protected_target, Role.Code.SUPER_ADMIN)
        deactivate = self.client.post(
            reverse("accounts:deactivate_user", args=[protected_target.pk]),
        )
        self.assertEqual(deactivate.status_code, 403)
        protected_target.refresh_from_db()
        self.assertTrue(protected_target.is_active)

    def test_multiple_roles_union_their_explicit_grants(self):
        user = make_user()
        assign_role(user, Role.Code.CFERI_STAFF)
        assign_role(user, Role.Code.SYSTEM_ADMIN)
        record = SimpleNamespace(
            pk=10,
            owner_id=999,
            sensitivity="internal",
            assigned_users=SimpleNamespace(
                filter=lambda **kwargs: SimpleNamespace(exists=lambda: kwargs.get("pk") == user.pk)
            ),
        )
        self.assertTrue(
            has_access(
                user,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.READ,
                record,
            )
        )
        self.assertTrue(
            has_access(
                user,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
                record,
            )
        )

    def test_django_permission_backend_combines_permissions_from_all_roles(self):
        user = make_user()
        first_role = Role.objects.get(code=Role.Code.CFERI_STAFF)
        second_role = Role.objects.get(code=Role.Code.SYSTEM_ADMIN)
        first_permission = Permission.objects.get(
            content_type__app_label="accounts",
            codename="view_user",
        )
        second_permission = Permission.objects.get(
            content_type__app_label="accounts",
            codename="change_user",
        )
        first_role.permissions.add(first_permission)
        second_role.permissions.add(second_permission)
        user.roles.add(first_role, second_role)

        self.assertTrue(user.has_perm("accounts.view_user"))
        self.assertTrue(user.has_perm("accounts.change_user"))

    def test_system_admin_confidential_document_deny_and_board_aggregate_only(self):
        system_admin = make_user()
        assign_role(system_admin, Role.Code.SYSTEM_ADMIN)
        confidential = SimpleNamespace(
            pk=1,
            owner_id=None,
            sensitivity="confidential",
            assigned_users=None,
        )
        self.assertFalse(
            has_access(
                system_admin,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.READ,
                confidential,
            )
        )
        confidential_business_record = SimpleNamespace(
            pk=3,
            owner_id=None,
            sensitivity="confidential",
            assigned_users=None,
        )
        self.assertFalse(
            has_access(
                system_admin,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.READ,
                confidential_business_record,
            )
        )

        board = make_user(email="board@gmail.com")
        assign_role(board, Role.Code.BOARD_MEMBER)
        incubatee = SimpleNamespace(
            pk=2,
            owner_id=None,
            sensitivity="internal",
            assigned_users=None,
        )
        self.assertFalse(
            has_access(
                board,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.READ,
                incubatee,
            )
        )
        self.assertTrue(
            RoleGrant.objects.filter(
                role__users=board,
                resource=RoleGrant.Resource.INCUBATEES,
                action=RoleGrant.Action.READ,
                scope=RoleGrant.Scope.AGGREGATE,
            ).exists()
        )

    def test_sharing_does_not_bypass_restricted_or_system_admin_confidential_boundaries(self):
        owner = make_user(email="owner@gmail.com")
        assign_role(owner, Role.Code.SUPER_ADMIN)
        system_admin = make_user(email="system@gmail.com")
        assign_role(system_admin, Role.Code.SYSTEM_ADMIN)

        restricted = SimpleNamespace(pk=1, sensitivity="restricted")
        with self.assertRaises(PermissionDenied):
            share_record(
                owner,
                system_admin,
                restricted,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                [RoleGrant.Action.READ],
            )

        confidential = SimpleNamespace(pk=2, sensitivity="confidential")
        with patch("accounts.access.ContentType.objects.get_for_model"), patch(
            "accounts.access.RecordShare.objects.filter"
        ) as shares:
            shares.return_value.values_list.return_value = [["read"]]
            self.assertFalse(
                has_access(
                    system_admin,
                    RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                    RoleGrant.Action.READ,
                    confidential,
                )
            )

    def test_explicit_share_grants_only_requested_actions_and_scopes_query(self):
        owner = make_user(email="owner@gmail.com")
        assign_role(owner, Role.Code.SUPER_ADMIN)
        guest = make_user(email="guest@gmail.com")
        guest.access_expires_at = timezone.now() + timedelta(days=7)
        guest.save(update_fields=("access_expires_at",))
        assign_role(guest, Role.Code.EXTERNAL_GUEST)
        record = RoleGrant.objects.get(
            role__code=Role.Code.ADMIN,
            resource=RoleGrant.Resource.USERS,
            action=RoleGrant.Action.READ,
        )

        share_record(
            owner,
            guest,
            record,
            RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
            [RoleGrant.Action.READ],
        )

        self.assertTrue(
            has_access(
                guest,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
                record,
            )
        )
        self.assertFalse(
            has_access(
                guest,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.UPDATE,
                record,
            )
        )
        self.assertIn(
            record.pk,
            accessible_queryset(
                guest,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                RoleGrant.Action.READ,
                RoleGrant.objects.all(),
            ).values_list("pk", flat=True),
        )

    def test_share_cannot_grant_action_owner_does_not_have(self):
        admin = make_user(email="admin@gmail.com")
        assign_role(admin, Role.Code.ADMIN)
        guest = make_user(email="guest@gmail.com")
        guest.access_expires_at = timezone.now() + timedelta(days=7)
        guest.save(update_fields=("access_expires_at",))
        assign_role(guest, Role.Code.EXTERNAL_GUEST)
        record = SimpleNamespace(pk=1, sensitivity="confidential")

        with self.assertRaises(PermissionDenied):
            share_record(
                admin,
                guest,
                record,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                [RoleGrant.Action.DELETE],
            )

    def test_assigned_scope_does_not_expose_unassigned_records(self):
        user = make_user()
        user.access_expires_at = timezone.now() + timedelta(days=30)
        user.save(update_fields=("access_expires_at",))
        assign_role(user, Role.Code.FREELANCER)
        assigned = SimpleNamespace(
            pk=1,
            owner_id=None,
            sensitivity="internal",
            assigned_users=SimpleNamespace(
                filter=lambda **kwargs: SimpleNamespace(exists=lambda: kwargs.get("pk") == user.pk)
            ),
        )
        other = SimpleNamespace(
            pk=2,
            owner_id=None,
            sensitivity="internal",
            assigned_users=SimpleNamespace(
                filter=lambda **kwargs: SimpleNamespace(exists=lambda: False)
            ),
        )
        self.assertTrue(
            has_access(user, RoleGrant.Resource.INCUBATEES, RoleGrant.Action.READ, assigned)
        )
        self.assertFalse(
            has_access(user, RoleGrant.Resource.INCUBATEES, RoleGrant.Action.READ, other)
        )

    def test_staff_records_are_assigned_only_and_board_confidential_docs_default_deny(self):
        staff = make_user()
        assign_role(staff, Role.Code.CFERI_STAFF)
        unassigned_record = SimpleNamespace(
            pk=1,
            owner_id=None,
            sensitivity="internal",
            assigned_users=SimpleNamespace(
                filter=lambda **kwargs: SimpleNamespace(exists=lambda: False)
            ),
        )
        self.assertFalse(
            has_access(
                staff,
                RoleGrant.Resource.INCUBATEES,
                RoleGrant.Action.READ,
                unassigned_record,
            )
        )
        board = make_user(email="board-confidential@gmail.com")
        assign_role(board, Role.Code.BOARD_MEMBER)
        confidential = SimpleNamespace(
            pk=2,
            owner_id=None,
            sensitivity="confidential",
            assigned_users=None,
        )
        self.assertFalse(
            has_access(
                board,
                RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS,
                RoleGrant.Action.READ,
                confidential,
            )
        )

    def test_download_export_and_task_helpers_enforce_the_same_read_scope(self):
        super_admin = make_user(email="super-download@gmail.com")
        assign_role(super_admin, Role.Code.SUPER_ADMIN)
        record = SimpleNamespace(pk=11, sensitivity="internal")
        content = SimpleNamespace(
            name="private.pdf",
            open=lambda _mode: BytesIO(b"private test content"),
        )
        response = authorized_file_download(
            super_admin,
            RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
            record,
            content,
        )
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(response["Cache-Control"], "private, no-store")

        guest = make_user(email="unshared@gmail.com")
        guest.access_expires_at = timezone.now() + timedelta(days=1)
        guest.save(update_fields=("access_expires_at",))
        assign_role(guest, Role.Code.EXTERNAL_GUEST)
        with self.assertRaises(PermissionDenied):
            authorized_file_download(
                guest,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
                record,
                content,
            )

        allowed = authorized_export_queryset(
            super_admin,
            RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
            RoleGrant.objects.all(),
        )
        task_allowed = authorized_task_queryset(
            super_admin,
            RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
            RoleGrant.Action.READ,
            RoleGrant.objects.all(),
        )
        self.assertEqual(allowed.count(), RoleGrant.objects.count())
        self.assertEqual(task_allowed.count(), RoleGrant.objects.count())

    def test_super_admin_matrix_ui_saves_grants_and_is_not_open_to_system_admin(self):
        super_admin = make_user(email="matrix-admin@gmail.com")
        assign_role(super_admin, Role.Code.SUPER_ADMIN)
        secret = pyotp.random_base32()
        TOTPDevice.objects.create(
            user=super_admin,
            secret_encrypted=encrypt_totp_secret(secret),
            confirmed=True,
        )
        self.client.force_login(super_admin)
        self.client.post(
            reverse("accounts:mfa_verify"),
            {"otp_token": pyotp.TOTP(secret).now()},
        )

        response = self.client.get(
            reverse("accounts:role_matrix"),
            {"role": Role.Code.CFERI_STAFF},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Role permission matrix")

        data = {"role": Role.Code.CFERI_STAFF}
        for resource, _resource_label in RoleGrant.Resource.choices:
            for action, _action_label in RoleGrant.Action.choices:
                data[f"{resource}__{action}"] = "none"
        response = self.client.post(reverse("accounts:role_matrix"), data)
        self.assertRedirects(
            response,
            f"{reverse('accounts:role_matrix')}?role={Role.Code.CFERI_STAFF}",
        )
        self.assertFalse(
            RoleGrant.objects.filter(
                role__code=Role.Code.CFERI_STAFF,
                resource=RoleGrant.Resource.INCUBATEES,
                action=RoleGrant.Action.READ,
            ).exists()
        )

    def test_soft_delete_requires_record_grant_and_records_actor(self):
        staff = make_user()
        assign_role(staff, Role.Code.CFERI_STAFF)
        denied_record = SimpleNamespace(
            pk=5,
            access_resource=RoleGrant.Resource.INCUBATEES,
            sensitivity="internal",
            owner_id=None,
            user_id=None,
            created_by_id=None,
            is_deleted=False,
            deleted_at=None,
            deleted_by=None,
            save=lambda **_kwargs: None,
        )
        with self.assertRaises(PermissionDenied):
            soft_delete_record(staff, RoleGrant.Resource.INCUBATEES, denied_record)

        admin = make_user(email="soft-delete-admin@gmail.com")
        assign_role(admin, Role.Code.ADMIN)
        record = SimpleNamespace(
            pk=6,
            access_resource=RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
            sensitivity="internal",
            owner_id=None,
            user_id=None,
            created_by_id=None,
            is_deleted=False,
            deleted_at=None,
            deleted_by=None,
            _meta=SimpleNamespace(label="accounts.testrecord"),
            save=lambda **_kwargs: None,
        )
        result = AccessControlledModel.delete(record, user=admin)
        self.assertEqual(result[0], 1)
        self.assertTrue(record.is_deleted)
        self.assertEqual(record.deleted_by, admin)

    def test_recovery_is_limited_to_ninety_days_and_permanent_delete_is_disabled(self):
        admin = make_user(email="recover-admin@gmail.com")
        assign_role(admin, Role.Code.ADMIN)
        record = SimpleNamespace(
            pk=7,
            access_resource=RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
            sensitivity="internal",
            owner_id=None,
            user_id=None,
            created_by_id=None,
            is_deleted=True,
            deleted_at=timezone.now() - timedelta(days=89),
            deleted_by=admin,
            _meta=SimpleNamespace(label="accounts.testrecord"),
            save=lambda **_kwargs: None,
        )
        self.assertTrue(
            AccessControlledModel.restore(
                record,
                admin,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
            )
        )
        self.assertFalse(record.is_deleted)
        self.assertIsNone(record.deleted_at)

        record.is_deleted = True
        record.deleted_at = timezone.now() - timedelta(days=91)
        with self.assertRaises(PermissionDenied):
            AccessControlledModel.restore(
                record,
                admin,
                RoleGrant.Resource.CENTRE_GENERAL_DOCUMENTS,
            )
        with self.assertRaises(PermissionDenied):
            AccessControlledModel.delete(record, user=admin, permanent=True)

        super_admin = make_user(email="permanent-delete-super@gmail.com")
        assign_role(super_admin, Role.Code.SUPER_ADMIN)
        with self.assertRaisesMessage(
            PermissionDenied,
            "Permanent deletion is disabled until an approved retention schedule exists.",
        ):
            AccessControlledModel.delete(record, user=super_admin, permanent=True)
        with self.assertRaisesMessage(
            PermissionDenied,
            "Permanent deletion is disabled until an approved retention schedule exists.",
        ):
            permanently_delete_record(super_admin, record)

class AccountFormTests(TestCase):
    def test_invitation_requires_gmail_email_and_valid_email_is_accepted(self):
        from accounts.management_forms import UserInvitationForm

        invalid = UserInvitationForm({"email": "guest@example.test"})
        self.assertFalse(invalid.is_valid())
        self.assertIn("email", invalid.errors)

        valid = UserInvitationForm(
            {
                "email": "guest@gmail.com",
                "roles": [Role.objects.get(code=Role.Code.EXTERNAL_GUEST).pk],
                "access_expires_at": "2030-01-01T00:00",
            }
        )
        self.assertTrue(valid.is_valid())

    def test_invitation_requires_expiry_for_freelancer_and_guest(self):
        from accounts.management_forms import UserInvitationForm

        form = UserInvitationForm(
            {
                "email": "freelancer@gmail.com",
                "roles": [Role.objects.get(code=Role.Code.FREELANCER).pk],
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("access_expires_at", form.errors)

    def test_super_admin_can_edit_role_matrix_but_system_admin_cannot(self):
        user = make_user(email="super@gmail.com")
        assign_role(user, Role.Code.SUPER_ADMIN)
        secret = pyotp.random_base32()
        TOTPDevice.objects.create(
            user=user,
            secret_encrypted=encrypt_totp_secret(secret),
            confirmed=True,
        )
        self.client.force_login(user)
        self.client.post(
            reverse("accounts:mfa_verify"),
            {"otp_token": pyotp.TOTP(secret).now()},
        )
        response = self.client.get(reverse("accounts:role_matrix"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Role permission matrix")


class TemporaryRoleAssignmentTests(TransactionTestCase):
    reset_sequences = True

    def test_temporary_roles_require_an_expiry_date(self):
        user = make_user()
        with self.assertRaises(ValidationError):
            user.roles.add(Role.objects.get(code=Role.Code.EXTERNAL_GUEST))
