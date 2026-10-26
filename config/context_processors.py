from accounts.access import has_access
from accounts.models import Role, RoleGrant


def sidebar_permissions(request):
    user = request.user
    if not user.is_authenticated:
        return {
            "can_manage_users": False,
            "can_manage_role_matrix": False,
            "can_view_reports": False,
            "can_view_audit_log": False,
        }

    return {
        "can_manage_users": user.is_superuser
        or user.roles.filter(
            code__in=(Role.Code.SUPER_ADMIN, Role.Code.SYSTEM_ADMIN)
        ).exists(),
        "can_manage_role_matrix": user.is_superuser
        or user.roles.filter(code=Role.Code.SUPER_ADMIN).exists(),
        "can_view_reports": has_access(
            user, RoleGrant.Resource.REPORTS, RoleGrant.Action.READ
        ),
        "can_view_audit_log": has_access(
            user, RoleGrant.Resource.AUDIT_LOG, RoleGrant.Action.READ
        ),
    }
