from unittest.mock import patch

from django.db import DatabaseError
from django.test import SimpleTestCase
from django.urls import reverse


class HealthCheckTests(SimpleTestCase):
    @patch("config.health.connection")
    def test_health_check_returns_ok_when_database_query_succeeds(self, database):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ok"})

    @patch("config.health.connection")
    def test_health_check_returns_503_when_database_is_unavailable(self, database):
        database.cursor.side_effect = DatabaseError
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"status": "unhealthy", "database": "unavailable"},
        )
