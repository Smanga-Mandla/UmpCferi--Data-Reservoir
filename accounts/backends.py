from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Permission


class RolePermissionBackend(ModelBackend):
    """Combine Django user/group permissions with the union of assigned role grants."""

    def user_can_authenticate(self, user):
        if user.is_access_expired:
            if user.is_active:
                user.is_active = False
                user.save(update_fields=("is_active",))
            return False
        return super().user_can_authenticate(user)

    def get_group_permissions(self, user_obj, obj=None):
        permissions = super().get_group_permissions(user_obj, obj)
        if obj is not None or not user_obj.is_active:
            return permissions

        role_permissions = Permission.objects.filter(
            cferi_roles__users=user_obj,
        ).values_list("content_type__app_label", "codename")
        permissions.update(f"{app_label}.{codename}" for app_label, codename in role_permissions)
        return permissions
