import logging
from smtplib import SMTPException

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from incubation.models import (
    IncubateePortalNotification,
    IncubateeSupportRequest,
)

logger = logging.getLogger(__name__)


def _email_notification(notification):
    notification.email_attempted_at = timezone.now()
    try:
        sent = send_mail(
            subject=f"UMP CFERI portal notification: {notification.title}",
            message=(
                f"{notification.title}\n\n{notification.message}\n\n"
                "Sign in to the UMP CFERI Incubatee Portal to view your business information."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL or None,
            recipient_list=[notification.user.email],
            fail_silently=False,
        )
        if sent != 1:
            raise SMTPException("The email backend did not report a sent message.")
    except (SMTPException, OSError) as error:
        notification.email_error = str(error)[:2000]
        notification.save(update_fields=("email_attempted_at", "email_error"))
        logger.exception(
            "Failed to email incubatee portal notification",
            extra={
                "notification_id": notification.pk,
                "user_id": notification.user_id,
            },
        )
        return
    notification.email_sent_at = timezone.now()
    notification.email_error = ""
    notification.save(
        update_fields=("email_attempted_at", "email_sent_at", "email_error")
    )


def notify_business_accounts(*, business, notification_type, title, message=""):
    user_ids = business.account_links.values_list("account__user_id", flat=True)
    notifications = [
        IncubateePortalNotification.objects.create(
            user_id=user_id,
            incubatee=business,
            notification_type=notification_type,
            title=title,
            message=message,
        )
        for user_id in user_ids.distinct()
    ]
    for notification in notifications:
        _email_notification(notification)
    return notifications


def notify_support_request(request, *, status_changed, response):
    if not status_changed and not response:
        return []
    message = "A CFERI staff member updated your support request."
    status_label = request.get_status_display()
    notification = IncubateePortalNotification.objects.create(
        user=request.requested_by,
        incubatee=request.incubatee,
        notification_type=IncubateePortalNotification.Type.SUPPORT_REQUEST,
        title=f"Support request update: {status_label}",
        message=message,
    )
    _email_notification(notification)
    return [notification]
