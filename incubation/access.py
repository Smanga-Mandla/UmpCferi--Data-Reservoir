from accounts.access import has_access
from accounts.models import Role, RoleGrant


PERSONAL_DATA_ROLES = {Role.Code.SUPER_ADMIN, Role.Code.ADMIN}


def role_codes(user):
    roles = set(user.roles.values_list("code", flat=True))
    if Role.Code.INCUBATEE in roles:
        return {Role.Code.INCUBATEE}
    return {Role.Code.SUPER_ADMIN} if user.is_superuser else roles


def can_view_founder_data(user, incubatee):
    if not has_access(
        user,
        RoleGrant.Resource.INCUBATEES,
        RoleGrant.Action.READ,
        incubatee,
    ):
        return False
    return bool(role_codes(user) & PERSONAL_DATA_ROLES)


def can_manage_incubatee_documents(user, incubatee, category=None):
    if not has_access(
        user,
        RoleGrant.Resource.INCUBATEES,
        RoleGrant.Action.READ,
        incubatee,
    ):
        return False
    # File retention/access policy for ID copies and financial evidence is open.
    return bool(role_codes(user) & PERSONAL_DATA_ROLES)


def is_aggregate_only(user):
    return Role.Code.BOARD_MEMBER in role_codes(user) and not (
        role_codes(user) - {Role.Code.BOARD_MEMBER}
    )
