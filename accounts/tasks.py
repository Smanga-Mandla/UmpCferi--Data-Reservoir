from celery import shared_task
from django.utils import timezone

from accounts.models import User


@shared_task
def deactivate_expired_accounts():
    return User.objects.filter(
        is_active=True,
        access_expires_at__lte=timezone.now(),
    ).update(is_active=False)
