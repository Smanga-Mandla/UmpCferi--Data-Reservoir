import logging

from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
def health_check(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        logger.exception("Database health check failed")
        return JsonResponse(
            {"status": "unhealthy", "database": "unavailable"},
            status=503,
        )

    return JsonResponse({"status": "ok", "database": "ok"})
