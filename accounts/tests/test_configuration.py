from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import storages
from django.test import SimpleTestCase
from django.urls import reverse

from config.settings.database import database_config_from_env
from config.settings.validation import validate_public_base_url


class ProjectConfigurationTests(SimpleTestCase):
    def test_neon_database_url_requires_ssl_and_disables_persistent_connections_by_default(self):
        database = database_config_from_env(
            {
                "DATABASE_URL": (
                    "postgresql://app:secret@ep-example-pooler.region.aws.neon.tech/"
                    "ump?sslmode=disable"
                )
            }
        )["default"]

        self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(database["HOST"], "ep-example-pooler.region.aws.neon.tech")
        self.assertEqual(database["CONN_MAX_AGE"], 0)
        self.assertTrue(database["CONN_HEALTH_CHECKS"])
        self.assertEqual(database["OPTIONS"]["sslmode"], "require")

    def test_postgres_environment_variables_remain_supported_without_database_url(self):
        database = database_config_from_env(
            {
                "POSTGRES_DB": "local_db",
                "POSTGRES_USER": "local_user",
                "POSTGRES_PASSWORD": "local_password",
                "POSTGRES_HOST": "localhost",
                "POSTGRES_PORT": "5433",
            }
        )["default"]

        self.assertEqual(database["NAME"], "local_db")
        self.assertEqual(database["USER"], "local_user")
        self.assertEqual(database["HOST"], "localhost")
        self.assertEqual(database["PORT"], "5433")
        self.assertEqual(database["OPTIONS"]["connect_timeout"], 5)

    def test_custom_email_user_and_postgresql_are_configured(self):
        self.assertEqual(settings.AUTH_USER_MODEL, "accounts.User")
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(settings.AUTHENTICATION_BACKENDS, ["accounts.backends.RolePermissionBackend"])
        self.assertEqual(
            settings.PASSWORD_HASHERS[0],
            "django.contrib.auth.hashers.Argon2PasswordHasher",
        )
        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 1800)
        self.assertEqual(settings.SESSION_COOKIE_AGE, 1209600)
        self.assertNotIn(
            "accounts.middleware.InactivityTimeoutMiddleware",
            settings.MIDDLEWARE,
        )
        self.assertEqual(settings.LOGOUT_REDIRECT_URL, "accounts:login")

    def test_private_media_is_separate_from_static_files(self):
        self.assertNotEqual(settings.PRIVATE_MEDIA_ROOT, settings.STATIC_ROOT)
        self.assertEqual(
            settings.STORAGES["default"]["BACKEND"],
            "config.storage.PrivateMediaStorage",
        )
        with self.assertRaises(ValueError):
            storages["default"].url("private-document.pdf")

    def test_celery_uses_configured_redis_url(self):
        self.assertTrue(settings.CELERY_BROKER_URL.startswith("redis://"))

    def test_production_public_base_url_requires_https_and_rejects_credentials(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_public_base_url("http://reservoir.example.test", require_https=True)
        with self.assertRaises(ImproperlyConfigured):
            validate_public_base_url(
                "https://user:secret@reservoir.example.test",
                require_https=True,
            )
        self.assertEqual(
            validate_public_base_url("https://reservoir.example.test/", require_https=True),
            "https://reservoir.example.test",
        )

    def test_root_redirects_to_login_and_login_page_renders(self):
        home = self.client.get(reverse("home"))
        self.assertRedirects(home, reverse("accounts:login"))
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Email address")
        self.assertContains(response, "Authenticator code")
        self.assertNotContains(response, "nav-signin-btn")
        self.assertNotContains(response, "placeholder=")

    def test_dashboard_requires_authentication(self):
        response = self.client.get(reverse("reporting:dashboard"))
        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('reporting:dashboard')}",
        )

    def test_role_dashboard_routes_exist(self):
        for name in (
            "super_admin",
            "system_admin",
            "admin",
            "board_member",
            "cferi_staff",
            "freelancer",
            "external_guest",
        ):
            with self.subTest(role=name):
                self.assertEqual(reverse(f"reporting:{name}").split("/")[-2], name.replace("_", "-"))
