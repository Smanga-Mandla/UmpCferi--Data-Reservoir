from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render

from accounts.access import accessible_queryset
from accounts.models import Role, RoleGrant
from incubation.models import Incubatee, IncubateeAccount


@login_required
def incubatee_dashboard(request):
    if not request.user.roles.filter(code=Role.Code.INCUBATEE).exists():
        raise PermissionDenied

    businesses = accessible_queryset(
        request.user,
        RoleGrant.Resource.INCUBATEES,
        RoleGrant.Action.READ,
        Incubatee.objects.all(),
    ).exclude(sensitivity__in=("confidential", "restricted")).order_by("legal_name")
    account = get_object_or_404(IncubateeAccount, user=request.user)
    linked_businesses = businesses.filter(
        account_links__account=account
    ).distinct()
    business_id = request.GET.get("business")
    if business_id:
        business = get_object_or_404(linked_businesses, pk=business_id)
    else:
        business = linked_businesses.first()

    context = {
        "account": account,
        "businesses": linked_businesses,
        "business": business,
    }
    if business is None:
        return render(request, "incubation/account_management/dashboard.html", context)

    return render(request, "incubation/account_management/dashboard.html", context)
