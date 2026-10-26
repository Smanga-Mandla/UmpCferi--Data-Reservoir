from accounts.models import Role, RoleGrant
from accounts.access import accessible_queryset
from incubation.models import Incubatee


MANAGEMENT_ROLES = {Role.Code.ADMIN, Role.Code.CFERI_STAFF}


def can_manage_incubatee_accounts(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_active
        and not user.is_access_expired
        and not user.roles.filter(code=Role.Code.INCUBATEE).exists()
        and user.roles.filter(code__in=MANAGEMENT_ROLES).exists()
    )


def manageable_businesses(user):
    if not can_manage_incubatee_accounts(user):
        return Incubatee.objects.none()
    if user.roles.filter(code=Role.Code.ADMIN).exists():
        return Incubatee.objects.all()
    readable = accessible_queryset(
        user,
        RoleGrant.Resource.INCUBATEES,
        RoleGrant.Action.READ,
        Incubatee.objects.all(),
    )
    return accessible_queryset(
        user,
        RoleGrant.Resource.INCUBATEES,
        RoleGrant.Action.UPDATE,
        readable,
    )


def manageable_account_businesses(user):
    """Return businesses available for representative account administration."""
    if not can_manage_incubatee_accounts(user):
        return Incubatee.objects.none()
    if user.roles.filter(code=Role.Code.ADMIN).exists():
        return Incubatee.objects.all()
    if user.roles.filter(code=Role.Code.CFERI_STAFF).exists():
        return Incubatee.objects.exclude(sensitivity="restricted")
    return Incubatee.objects.none()
