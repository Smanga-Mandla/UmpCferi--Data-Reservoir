from types import SimpleNamespace
from unittest.mock import Mock

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from reporting.views import RoleDashboardView


class RoleDashboardAccessTests(SimpleTestCase):
    def test_anonymous_user_is_redirected_to_login(self):
        request = RequestFactory().get(reverse("reporting:super_admin"))
        request.user = AnonymousUser()

        response = RoleDashboardView.as_view(role_code="super_admin")(request)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_authenticated_user_without_role_is_forbidden(self):
        roles = Mock()
        roles.filter.return_value.exists.return_value = False
        request = RequestFactory().get(reverse("reporting:super_admin"))
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            roles=roles,
        )

        with self.assertRaises(PermissionDenied):
            RoleDashboardView.as_view(role_code="super_admin")(request)
