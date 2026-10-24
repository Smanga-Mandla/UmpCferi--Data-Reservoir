import hashlib
import logging
import secrets
from calendar import monthrange
from datetime import date, timedelta
from smtplib import SMTPException

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from incubation.models import (
    CheckInAccessLink,
    CheckInReminder,
    Incubatee,
    IncubateeExit,
    IncubateeFollowUp,
    MonthlyCheckIn,
)

logger = logging.getLogger(__name__)


def _previous_month_start(today):
    if today.month == 1:
        return date(today.year - 1, 12, 1)
    return date(today.year, today.month - 1, 1)


def _date_in_month(year, month, day):
    return date(year, month, min(day, monthrange(year, month)[1]))


def _followup_due_date(exit_date, months):
    month_index = exit_date.month - 1 + months
    year = exit_date.year + month_index // 12
    month = month_index % 12 + 1
    return _date_in_month(year, month, exit_date.day)


def _checkin_recipients(incubatee):
    return sorted(
        {
            email.strip().lower()
            for email in incubatee.founders.filter(is_active=True)
            .select_related("founder__stakeholder")
            .values_list("founder__stakeholder__email", flat=True)
            if email and email.strip()
        }
    )


def _link_url(link, token):
    path = reverse(
        "incubation:public_checkin",
        kwargs={"link_id": link.pk},
    )
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{path}?token={token}"


def _create_or_refresh_link(checkin, email, now):
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expiry = now + timedelta(seconds=settings.INCUBATEE_CHECKIN_LINK_MAX_AGE_SECONDS)
    link, created = CheckInAccessLink.objects.get_or_create(
        checkin=checkin,
        recipient_email=email,
        defaults={"token_digest": digest, "expires_at": expiry},
    )
    if not created and link.used_at is None and link.expires_at <= now:
        link.token_digest = digest
        link.expires_at = expiry
        link.email_sent_at = None
        link.email_error = ""
        link.save(
            update_fields=("token_digest", "expires_at", "email_sent_at", "email_error")
        )
    elif not created:
        return link, None
    return link, token


def _send_link_email(link, token, *, reminder=False):
    subject = "UMP CFERI monthly business check-in reminder" if reminder else "UMP CFERI monthly business check-in"
    body = (
        "Please complete your monthly business check-in using this secure, single-use link:\n\n"
        f"{_link_url(link, token)}\n\n"
        f"The link expires at {timezone.localtime(link.expires_at).isoformat()} and should not be forwarded."
    )
    sent = send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL or None,
        recipient_list=[link.recipient_email],
        fail_silently=False,
    )
    if sent != 1:
        raise SMTPException("The email backend did not report a sent message.")


@shared_task
def send_monthly_checkin_links():
    today = timezone.localdate()
    send_day = settings.INCUBATEE_CHECKIN_SEND_DAY
    if today.day < send_day:
        return {"created": 0, "emailed": 0, "failed": 0, "skipped": 0}

    period_start = _previous_month_start(today)
    statuses = settings.INCUBATEE_CHECKIN_STATUSES
    due_day = settings.INCUBATEE_CHECKIN_DEADLINE_DAY
    due_date = _date_in_month(today.year, today.month, due_day)
    created_count = emailed_count = failed_count = skipped_count = 0
    incubatees = Incubatee.objects.filter(status__in=statuses).prefetch_related(
        "founders__founder__stakeholder"
    )
    for incubatee in incubatees:
        checkin, created = MonthlyCheckIn.objects.get_or_create(
            incubatee=incubatee,
            period_start=period_start,
            defaults={"due_date": due_date},
        )
        created_count += int(created)
        recipients = _checkin_recipients(incubatee)
        if not recipients:
            logger.error(
                "Monthly check-in has no founder email recipient",
                extra={"incubatee_id": str(incubatee.pk), "period_start": period_start.isoformat()},
            )
            failed_count += 1
            continue
        any_email_sent = False
        for email in recipients:
            link, token = _create_or_refresh_link(checkin, email, timezone.now())
            if token is None:
                if link.email_sent_at:
                    skipped_count += 1
                    continue
                token = secrets.token_urlsafe(32)
                link.token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                link.expires_at = timezone.now() + timedelta(
                    seconds=settings.INCUBATEE_CHECKIN_LINK_MAX_AGE_SECONDS
                )
                link.save(update_fields=("token_digest", "expires_at"))
            try:
                _send_link_email(link, token)
            except (SMTPException, OSError) as error:
                link.email_error = str(error)[:2000]
                link.save(update_fields=("email_error",))
                logger.exception(
                    "Failed to email a monthly incubatee check-in",
                    extra={"incubatee_id": str(incubatee.pk), "checkin_id": checkin.pk},
                )
                failed_count += 1
            else:
                link.email_sent_at = timezone.now()
                link.email_error = ""
                link.save(update_fields=("email_sent_at", "email_error"))
                emailed_count += 1
                any_email_sent = True
        if any_email_sent and checkin.sent_at is None:
            checkin.sent_at = timezone.now()
            checkin.save(update_fields=("sent_at",))

    return {
        "created": created_count,
        "emailed": emailed_count,
        "failed": failed_count,
        "skipped": skipped_count,
    }


@shared_task
def send_checkin_reminders():
    today = timezone.localdate()
    reminder_days = settings.INCUBATEE_CHECKIN_REMINDER_DAYS
    if not reminder_days:
        return {"emailed": 0, "failed": 0}
    pending_links = CheckInAccessLink.objects.filter(
        checkin__submitted_at__isnull=True,
        checkin__due_date__isnull=False,
        used_at__isnull=True,
        expires_at__gt=timezone.now(),
        email_sent_at__isnull=False,
    ).select_related("checkin", "checkin__incubatee")
    emailed_count = failed_count = 0
    for link in pending_links:
        days_before_due = (link.checkin.due_date - today).days
        for reminder_day in reminder_days:
            if days_before_due < 0 or days_before_due > reminder_day:
                continue
            reminder, _created = CheckInReminder.objects.get_or_create(
                access_link=link,
                days_before_due=reminder_day,
            )
            if reminder.sent_at:
                continue
            token = secrets.token_urlsafe(32)
            link.token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            link.expires_at = timezone.now() + timedelta(
                seconds=settings.INCUBATEE_CHECKIN_LINK_MAX_AGE_SECONDS
            )
            link.save(update_fields=("token_digest", "expires_at"))
            try:
                _send_link_email(link, token, reminder=True)
            except (SMTPException, OSError) as error:
                reminder.email_error = str(error)[:2000]
                reminder.save(update_fields=("email_error",))
                logger.exception(
                    "Failed to send a monthly check-in reminder",
                    extra={"checkin_id": link.checkin_id, "reminder_id": reminder.pk},
                )
                failed_count += 1
            else:
                reminder.sent_at = timezone.now()
                reminder.email_error = ""
                reminder.save(update_fields=("sent_at", "email_error"))
                emailed_count += 1
    return {"emailed": emailed_count, "failed": failed_count}


@shared_task
def create_due_incubatee_followups():
    today = timezone.localdate()
    created_count = 0
    for exit_record in IncubateeExit.objects.select_related("incubatee"):
        for months in (6, 12):
            due_date = _followup_due_date(exit_record.exit_date, months)
            if due_date > today:
                continue
            _followup, created = IncubateeFollowUp.objects.get_or_create(
                exit_record=exit_record,
                months_after_exit=months,
                defaults={"due_date": due_date},
            )
            created_count += int(created)
    return {"created": created_count}
