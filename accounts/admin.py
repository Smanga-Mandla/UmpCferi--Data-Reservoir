from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from accounts.models import Role, User


def _can_manage_access(request):
    return request.user.is_superuser or request.user.roles.filter(
        code__in=(Role.Code.SUPER_ADMIN, Role.Code.SYSTEM_ADMIN)
    ).exists()


def _is_super_admin(request):
    return request.user.is_superuser or request.user.roles.filter(
        code=Role.Code.SUPER_ADMIN
    ).exists()


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("code",)
    readonly_fields = ("code", "permissions")

    def has_module_permission(self, request):
        return _is_super_admin(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    def has_module_permission(self, request):
        return _can_manage_access(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        if not _can_manage_access(request):
            return False
        if obj is not None and obj.pk == request.user.pk:
            return True
        return _can_manage_access(request)
