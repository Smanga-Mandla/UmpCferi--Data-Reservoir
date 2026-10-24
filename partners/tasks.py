import logging
from datetime import timedelta
from smtplib import SMTPException

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from partners.models import MOU, MOUExpiryAlert

logger = logging.getLogger(__name__)


@shared_task
def send_mou_expiry_alerts():
    today = timezone.localdate()
    recipients = settings.MOU_ALERT_RECIPIENTS
    created_count = 0
    emailed_count = 0
    failed_count = 0

    mous = MOU.objects.filter(
        status=MOU.Status.ACTIVE,
        partner__is_deleted=False,
        end_date__gte=today,
        end_date__lte=today + timedelta(days=90),
    ).select_related("partner")

    for mou in mous:
        for days_before, _label in MOUExpiryAlert.DAYS_BEFORE_CHOICES:
            due_date = mou.end_date - timedelta(days=days_before)
            if due_date > today:
                continue
            alert, created = MOUExpiryAlert.objects.get_or_create(
                mou=mou,
                end_date=mou.end_date,
                days_before=days_before,
                defaults={"due_date": due_date},
            )
            if not created and alert.email_sent_at:
                continue
            if created:
                created_count += 1
            if mou.sensitivity in {"confidential", "restricted"}:
                alert.email_error = (
                    "Email was suppressed because no sensitivity-aware recipient policy "
                    "is configured. The dashboard alert remains available to authorized users."
                )
                alert.save(update_fields=("email_error",))
                logger.warning(
                    "MOU renewal alert email suppressed for sensitive agreement",
                    extra={"mou_id": str(mou.pk), "alert_id": alert.pk},
                )
                failed_count += 1
                continue
            if not recipients:
                alert.email_error = (
                    "No MOU_ALERT_RECIPIENTS configured; dashboard alert was "
                    "created but email was not sent."
                )
                alert.save(update_fields=("email_error",))
                logger.error(
                    "MOU renewal alert created without email recipients",
                    extra={"mou_id": str(mou.pk), "alert_id": alert.pk},
                )
                failed_count += 1
                continue
            try:
                sent = send_mail(
                    subject=f"UMP CFERI MOU renewal reminder: {mou.title}",
                    message=(
                        f'The MOU "{mou.title}" with {mou.partner.name} '
                        f"ends on {mou.end_date}. This is the scheduled "
                        f"{days_before}-day expiry and renewal reminder."
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL or None,
                    recipient_list=recipients,
                    fail_silently=False,
                )
                if sent != 1:
                    raise SMTPException("The email backend did not report a sent message.")
            except (SMTPException, OSError) as error:
                alert.email_error = str(error)[:2000]
                alert.save(update_fields=("email_error",))
                logger.exception(
                    "Failed to email MOU renewal alert",
                    extra={"mou_id": str(mou.pk), "alert_id": alert.pk},
                )
                failed_count += 1
            else:
                alert.email_sent_at = timezone.now()
                alert.email_error = ""
                alert.save(update_fields=("email_sent_at", "email_error"))
                emailed_count += 1

    return {
        "created": created_count,
        "emailed": emailed_count,
        "failed": failed_count,
    }
