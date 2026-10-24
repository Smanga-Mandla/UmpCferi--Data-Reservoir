import logging
import smtplib
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from accounts.models import Role, User
from reporting.metrics import impact_report

logger = logging.getLogger(__name__)


def _previous_month(period_end):
    first_of_current_month = period_end.replace(day=1)
    previous_month_end = first_of_current_month - timedelta(days=1)
    return previous_month_end.replace(day=1), previous_month_end


@shared_task
def send_monthly_summary_reports():
    today = timezone.localdate()
    period_start, period_end = _previous_month(today)
    recipients = (
        User.objects.filter(
            is_active=True,
            roles__code__in=(Role.Code.ADMIN, Role.Code.BOARD_MEMBER),
        )
        .distinct()
        .order_by("email")
    )
    if not recipients.exists():
        logger.info("Monthly reporting task found no active Admin or Board recipients.")
        return {"sent": 0, "failed": 0}

    sent = failed = 0
    for recipient in recipients:
        report = impact_report(recipient, date_from=period_start, date_to=period_end)
        business_count = (
            str(report["business_count"])
            if report["business_count"] is not None
            else "Suppressed for privacy"
        )
        jobs_created = (
            str(report["jobs_created"])
            if report["jobs_created"] is not None
            else "Suppressed for privacy"
        )
        funding = (
            f"R {report['funding_raised_zar']:.2f}"
            if report["funding_raised_zar"] is not None
            else "Suppressed for privacy"
        )
        message = (
            f"UMP CFERI monthly impact summary\n"
            f"Reporting period: {period_start.isoformat()} to {period_end.isoformat()}\n"
            f"Businesses represented: {business_count}\n"
            f"Jobs created: {jobs_created}\n"
            f"Funding received: {funding}\n"
        )
        try:
            send_mail(
                subject=f"UMP CFERI monthly summary: {period_start:%B %Y}",
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient.email],
                fail_silently=False,
            )
            sent += 1
        except (smtplib.SMTPException, OSError):
            failed += 1
            logger.exception(
                "Monthly report delivery failed for recipient id=%s",
                recipient.pk,
            )
    return {"sent": sent, "failed": failed}
