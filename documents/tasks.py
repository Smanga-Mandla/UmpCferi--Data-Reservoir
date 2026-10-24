import logging
from datetime import timedelta
from smtplib import SMTPException

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from documents.models import Document, ExpiryAlert

logger = logging.getLogger(__name__)


@shared_task
def send_expiry_alerts():
    today = timezone.localdate()
    created_count = 0
    emailed_count = 0
    failed_count = 0
    recipients = settings.DOCUMENT_EXPIRY_ALERT_RECIPIENTS

    documents = Document.objects.filter(
        archived=False,
        expiry_date__gte=today,
        expiry_date__lte=today + timedelta(days=60),
    ).select_related("owner")

    for document in documents:
        for days_before, _label in ExpiryAlert.DAYS_BEFORE_CHOICES:
            due_date = document.expiry_date - timedelta(days=days_before)
            if due_date > today:
                continue
            alert, created = ExpiryAlert.objects.get_or_create(
                document=document,
                expiry_date=document.expiry_date,
                days_before=days_before,
                defaults={"due_date": due_date},
            )
            if not created and alert.email_sent_at:
                continue
            if created:
                created_count += 1
            if not recipients:
                alert.email_error = (
                    "No DOCUMENT_EXPIRY_ALERT_RECIPIENTS configured; "
                    "dashboard alert was created but email was not sent."
                )
                alert.save(update_fields=("email_error",))
                logger.error(
                    "Centre document expiry alert created without email recipients",
                    extra={"document_id": str(document.pk), "alert_id": alert.pk},
                )
                failed_count += 1
                continue
            try:
                sent = send_mail(
                    subject=f"UMP CFERI document expires on {document.expiry_date}",
                    message=(
                        f'The centre document "{document.title}" '
                        f"({document.get_category_display()}) expires on "
                        f"{document.expiry_date}. This is the scheduled "
                        f"{days_before}-day expiry reminder."
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
                    "Failed to email centre document expiry alert",
                    extra={"document_id": str(document.pk), "alert_id": alert.pk},
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
