from accounts.access import has_access
from accounts.models import Role, RoleGrant


PROFILE_ACCESS_ROLES = {Role.Code.SUPER_ADMIN, Role.Code.ADMIN}
CONTACT_ACCESS_ROLES = PROFILE_ACCESS_ROLES | {Role.Code.CFERI_STAFF}


def _role_codes(user):
    if user.is_superuser:
        return {Role.Code.SUPER_ADMIN}
    return set(user.roles.values_list("code", flat=True))


def can_manage_sensitive_profiles(user):
    return bool(_role_codes(user) & PROFILE_ACCESS_ROLES)


def can_view_contact_details(user, stakeholder=None):
    roles = _role_codes(user)
    if roles & PROFILE_ACCESS_ROLES:
        return True
    return bool(
        stakeholder
        and Role.Code.CFERI_STAFF in roles
        and has_access(
            user,
            RoleGrant.Resource.STAKEHOLDERS,
            RoleGrant.Action.READ,
            stakeholder,
        )
    )
