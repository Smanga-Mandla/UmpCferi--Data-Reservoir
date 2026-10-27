import secrets

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.forms import UserPasswordResetForm
from accounts.models import Role
from audit.models import AuditEvent
from audit.services import log_event
from incubation.account_access import (
    can_manage_incubatee_accounts,
    manageable_account_businesses,
)
from incubation.account_forms import IncubateeAccountForm, IncubateeAccountSearchForm
from incubation.baselines import capture_incubatee_baseline
from incubation.models import (
    Incubatee,
    IncubateeAccount,
    IncubateeAccountBusiness,
    IncubateeBaselineSnapshot,
    IncubateeStatusHistory,
)

User = get_user_model()


def _require_account_manager(user):
    if not can_manage_incubatee_accounts(user):
        raise PermissionDenied


def _visible_accounts(user, business_queryset):
    accounts = IncubateeAccount.objects.select_related("user", "created_by")
    if user.roles.filter(code=Role.Code.ADMIN).exists():
        return accounts.prefetch_related("business_links__incubatee")
    return accounts.filter(
        business_links__incubatee__in=business_queryset
    ).distinct().prefetch_related("business_links__incubatee")


def incubatee_account_list(request):
    _require_account_manager(request.user)
    businesses = manageable_account_businesses(request.user)
    form = IncubateeAccountSearchForm(request.GET or None)
    accounts = _visible_accounts(request.user, businesses)
    if form.is_valid() and form.cleaned_data["q"]:
        query = form.cleaned_data["q"]
        accounts = accounts.filter(
            Q(user__first_name__icontains=query)
            | Q(user__last_name__icontains=query)
            | Q(user__email__icontains=query)
            | Q(business_links__incubatee__legal_name__icontains=query)
            | Q(business_links__incubatee__trading_name__icontains=query)
        ).distinct()
    account_rows = []
    manageable_ids = set(businesses.values_list("pk", flat=True))
    for account in accounts.order_by("user__last_name", "user__first_name", "user__email"):
        all_links = list(account.business_links.all())
        links = [link for link in all_links if link.incubatee_id in manageable_ids]
        if len(links) != len(all_links):
            continue
        account_rows.append(
            {
                "account": account,
                "businesses": [link.incubatee for link in links],
                "invitation_status": account.invitation_status,
                "account_status": account.account_status,
            }
        )
    return render(
        request,
        "incubation/account_management/list.html",
        {
            "account_rows": account_rows,
            "search_form": form,
            "search_query": form.cleaned_data.get("q", "") if form.is_valid() else "",
            "can_add_account": True,
            "has_manageable_businesses": businesses.exists(),
        },
    )


def incubatee_account_create(request):
    _require_account_manager(request.user)
    businesses = manageable_account_businesses(request.user)
    form = IncubateeAccountForm(
        request.POST or None,
        business_queryset=businesses,
        business_query=request.GET.get("business_q", ""),
        actor=request.user,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        full_name_parts = data["full_name"].split(maxsplit=1)
        first_name, last_name = full_name_parts[0], full_name_parts[1]
        with transaction.atomic():
            if (
                data["registration_mode"]
                == IncubateeAccountForm.RegistrationMode.NEW_BUSINESS
                and Incubatee.all_objects.filter(
                    Q(legal_name__iexact=data["new_business_name"])
                    | Q(trading_name__iexact=data["new_business_name"])
                ).exists()
            ):
                form.add_error(
                    "new_business_name",
                    "A business with this name already exists. Search for and link the existing business.",
                )
                return render(
                    request,
                    "incubation/account_management/form.html",
                    {
                        "form": form,
                        "title": "Add incubatee",
                        "business_query": request.POST.get("business_q", ""),
                    },
                )
            user = User.objects.create_user(
                email=data["email"],
                password=secrets.token_urlsafe(48),
                first_name=first_name,
                last_name=last_name,
            )
            user.roles.add(Role.objects.get(code=Role.Code.INCUBATEE))
            account = IncubateeAccount.objects.create(
                user=user,
                entrepreneur_category=data["entrepreneur_category"],
                created_by=request.user,
            )
            if (
                data["registration_mode"]
                == IncubateeAccountForm.RegistrationMode.NEW_BUSINESS
            ):
                business = Incubatee.objects.create(
                    legal_name=data["new_business_name"],
                    products_services=data["business_description"].strip(),
                    business_registration_status=data["business_registration_status"],
                    cipc_registration_number=data["cipc_registration_number"],
                    date_registered=data["date_registered"],
                    business_stage=data["business_stage"],
                    programme=data["programme"],
                    staff_owner=data["assigned_staff"],
                    top_support_needs=(
                        [data["initial_support_needs"].strip()]
                        if data["initial_support_needs"].strip()
                        else []
                    ),
                    status=Incubatee.Status.APPLICANT,
                    created_by=request.user,
                    updated_by=request.user,
                    owner=request.user,
                    source="staff_incubatee_registration",
                )
                business.assigned_users.add(data["assigned_staff"])
                IncubateeStatusHistory.objects.create(
                    incubatee=business,
                    old_status="",
                    new_status=business.status,
                    changed_by=request.user,
                )
                IncubateeAccountBusiness.objects.create(
                    account=account,
                    incubatee=business,
                    linked_by=request.user,
                )
                capture_incubatee_baseline(
                    business,
                    request.user,
                    source="staff_incubatee_registration",
                )
                business_ids = [str(business.pk)]
                log_event(
                    actor=request.user,
                    action=AuditEvent.Action.CREATE,
                    target=business,
                    resource="incubation.incubatee",
                    metadata={
                        "source": "staff_incubatee_registration",
                        "registration_status": business.business_registration_status,
                        "business_stage": business.business_stage,
                    },
                    request=request,
                )
            else:
                IncubateeAccountBusiness.objects.bulk_create(
                    [
                        IncubateeAccountBusiness(
                            account=account,
                            incubatee=business,
                            linked_by=request.user,
                        )
                        for business in data["businesses"]
                    ]
                )
                for business in data["businesses"]:
                    if not IncubateeBaselineSnapshot.objects.filter(
                        incubatee=business
                    ).exists():
                        capture_incubatee_baseline(
                            business,
                            request.user,
                            source="staff_existing_business_link",
                        )
                business_ids = [
                    str(business.pk) for business in data["businesses"]
                ]
            reset_form = UserPasswordResetForm({"email": user.email})
            if not reset_form.is_valid():
                raise RuntimeError("The account activation email could not be prepared.")
            reset_form.save(
                request=request,
                use_https=request.is_secure(),
                token_generator=default_token_generator,
                email_template_name="accounts/invitation_email.html",
                subject_template_name="accounts/invitation_subject.txt",
            )
            account.invitation_sent_at = timezone.now()
            account.save(update_fields=("invitation_sent_at",))
            if data["account_status"] == "suspended":
                user.is_active = False
                user.save(update_fields=("is_active",))
            log_event(
                actor=request.user,
                action=AuditEvent.Action.CREATE,
                target=account,
                resource="incubation.incubatee_account",
                metadata={
                    "entrepreneur_category": account.entrepreneur_category,
                    "registration_mode": data["registration_mode"],
                    "business_ids": business_ids,
                    "status": data["account_status"],
                    "invitation_sent": True,
                },
                request=request,
            )
        messages.success(
            request,
            f"Incubatee invitation sent to {user.email}. The account can set its own password using the secure link.",
        )
        return redirect("incubation:account_list")
    return render(
        request,
        "incubation/account_management/form.html",
        {
            "form": form,
            "title": "Add incubatee",
            "business_query": request.GET.get("business_q", ""),
        },
    )


def _managed_account(request, account_id):
    businesses = manageable_account_businesses(request.user)
    account = get_object_or_404(
        _visible_accounts(request.user, businesses),
        pk=account_id,
    )
    if not account.user.roles.filter(code=Role.Code.INCUBATEE).exists():
        raise PermissionDenied
    if request.user.roles.filter(code=Role.Code.ADMIN).exists():
        return account
    business_ids = set(
        account.business_links.values_list("incubatee_id", flat=True)
    )
    if not business_ids.issubset(set(businesses.values_list("pk", flat=True))):
        raise PermissionDenied
    return account


@require_POST
def resend_incubatee_invitation(request, account_id):
    _require_account_manager(request.user)
    account = _managed_account(request, account_id)
    if account.invitation_status != "expired":
        raise PermissionDenied("Only expired incubatee invitations can be resent.")
    if not account.user.is_active:
        raise PermissionDenied("Reactivate this account before resending its invitation.")
    reset_form = UserPasswordResetForm({"email": account.user.email})
    if not reset_form.is_valid():
        raise RuntimeError("The account activation email could not be prepared.")
    reset_form.save(
        request=request,
        use_https=request.is_secure(),
        token_generator=default_token_generator,
        email_template_name="accounts/invitation_email.html",
        subject_template_name="accounts/invitation_subject.txt",
    )
    account.invitation_sent_at = timezone.now()
    account.save(update_fields=("invitation_sent_at",))
    log_event(
        actor=request.user,
        action=AuditEvent.Action.UPDATE,
        target=account,
        resource="incubation.incubatee_account",
        metadata={"action": "invitation_resent", "invitation_sent": True},
        request=request,
    )
    messages.success(request, f"A new activation link was sent to {account.user.email}.")
    return redirect("incubation:account_list")


@require_POST
def set_incubatee_account_status(request, account_id):
    _require_account_manager(request.user)
    account = _managed_account(request, account_id)
    if account.user_id == request.user.pk:
        raise PermissionDenied("You cannot change your own account status.")
    status = request.POST.get("status")
    if status not in {"active", "suspended"}:
        raise PermissionDenied("Choose a valid account status.")
    is_active = status == "active"
    if account.user.is_active == is_active:
        messages.info(request, f"The incubatee account is already {status}.")
        return redirect("incubation:account_list")
    account.user.is_active = is_active
    account.user.save(update_fields=("is_active",))
    log_event(
        actor=request.user,
        action=AuditEvent.Action.UPDATE,
        target=account,
        resource="incubation.incubatee_account",
        metadata={"action": "account_status_changed", "status": status},
        request=request,
    )
    messages.success(request, f"Incubatee account {status}.")
    return redirect("incubation:account_list")
