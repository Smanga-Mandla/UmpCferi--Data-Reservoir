from django.db.models import QuerySet

from accounts.access import accessible_queryset
from accounts.models import Role, RoleGrant
from incubation.models import Incubatee


def report_accessible_businesses(user, queryset=None) -> QuerySet:
    businesses = accessible_queryset(
        user,
        RoleGrant.Resource.REPORTS,
        RoleGrant.Action.READ,
        queryset if queryset is not None else Incubatee.objects.all(),
    )
    if not (
        user.is_superuser
        or user.roles.filter(code=Role.Code.SUPER_ADMIN).exists()
    ):
        businesses = businesses.exclude(
            sensitivity=Incubatee.Sensitivity.RESTRICTED
        )
    return businesses
