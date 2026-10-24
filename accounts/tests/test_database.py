import os
import unittest

from django.db import connection


@unittest.skipUnless(
    os.environ.get("RUN_DB_CONNECTIVITY_TESTS") == "1",
    "Set RUN_DB_CONNECTIVITY_TESTS=1 to verify a live PostgreSQL connection.",
)
class PostgreSQLConnectivityTests(unittest.TestCase):
    def test_configured_postgresql_accepts_a_query(self):
        self.assertEqual(connection.vendor, "postgresql")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone(), (1,))
