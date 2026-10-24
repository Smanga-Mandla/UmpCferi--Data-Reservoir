from functools import wraps

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import Http404

from accounts.models import RecordShare, Role, RoleGrant

SUPER_ADMIN = Role.Code.SUPER_ADMIN
SYSTEM_ADMIN = Role.Code.SYSTEM_ADMIN
BOARD_MEMBER = Role.Code.BOARD_MEMBER


def _role_codes(user):
    if user.is_superuser:
        return {SUPER_ADMIN}
    return set(user.roles.values_list("code", flat=True))


def _is_expired(user):
    if not user.is_active:
        return True
    if user.is_access_expired:
        user.is_active = False
        user.save(update_fields=("is_active",))
        return True
    return False


def _security_denies(user, record, resource, action, roles):
    sensitivity = getattr(record, "sensitivity", None)
    if sensitivity == "restricted" and SUPER_ADMIN not in roles:
        return True
    non_system_direct = RoleGrant.objects.filter(
        role__code__in=roles - {SYSTEM_ADMIN, BOARD_MEMBER},
        resource=resource,
        action=action,
        scope__in=(RoleGrant.Scope.ALL, RoleGrant.Scope.ASSIGNED, RoleGrant.Scope.OWN),
    ).exists()
    if (
        sensitivity == "confidential"
        and SYSTEM_ADMIN in roles
        and not non_system_direct
    ):
        return True
    if (
        sensitivity == "confidential"
        and BOARD_MEMBER in roles
        and resource == RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS
        and not non_system_direct
    ):
        # The SRS matrix grants Board read, but its role summary limits this to
        # governance documents. Keep the narrower boundary pending CFERI's decision.
        return True
    non_board_direct = RoleGrant.objects.filter(
        role__code__in=roles - {BOARD_MEMBER},
        resource=resource,
        action=action,
        scope__in=(RoleGrant.Scope.ALL, RoleGrant.Scope.ASSIGNED, RoleGrant.Scope.OWN),
    ).exists()
    if (
        BOARD_MEMBER in roles
        and resource in {RoleGrant.Resource.INCUBATEES, RoleGrant.Resource.CHECKINS_METRICS}
        and not non_board_direct
    ):
        return True
    return False


def has_access(user, resource, action, record=None):
    """Authorize one action from the baseline role matrix or an explicit share."""
    if not getattr(user, "is_authenticated", False) or _is_expired(user):
        return False

    roles = _role_codes(user)
    if not roles:
        return False

    grants = RoleGrant.objects.filter(
        role__code__in=roles,
        resource=resource,
        action=action,
    )
    scopes = set(grants.values_list("scope", flat=True))

    if record is None:
        return bool(scopes - {RoleGrant.Scope.AGGREGATE, RoleGrant.Scope.SYSTEM_HEALTH})

    if _security_denies(user, record, resource, action, roles):
        return False

    if RoleGrant.Scope.ALL in scopes:
        return True
    if RoleGrant.Scope.AGGREGATE in scopes or RoleGrant.Scope.SYSTEM_HEALTH in scopes:
        return False

    pk = getattr(record, "pk", None)
    if pk is None:
        return False
    if RoleGrant.Scope.OWN in scopes and (
        getattr(record, "owner_id", None) == user.pk
        or getattr(record, "user_id", None) == user.pk
        or getattr(record, "created_by_id", None) == user.pk
    ):
        return True
    if RoleGrant.Scope.ASSIGNED in scopes and _is_assigned(record, user):
        return True

    if RoleGrant.Scope.LIMITED in scopes and _is_assigned(record, user):
        return True

    if not hasattr(record, "_meta"):
        return False
    content_type = ContentType.objects.get_for_model(record, for_concrete_model=False)
    return any(
        action in shared_actions
        for shared_actions in RecordShare.objects.filter(
        content_type=content_type,
        object_id=str(pk),
        user=user,
    ).values_list("actions", flat=True)
    )


def _is_assigned(record, user):
    assigned_users = getattr(record, "assigned_users", None)
    if assigned_users is not None and hasattr(assigned_users, "filter"):
        if assigned_users.filter(pk=user.pk).exists():
            return True
    return getattr(record, "owner_id", None) == user.pk


def require_access(user, resource, action, record=None):
    if not has_access(user, resource, action, record):
        raise PermissionDenied
    return True


def soft_delete_record(user, resource, record):
    require_access(user, resource, RoleGrant.Action.DELETE, record)
    return record.delete(user=user)


def permanently_delete_record(user, record):
    if not getattr(user, "is_authenticated", False) or not (
        user.is_superuser or user.roles.filter(code=SUPER_ADMIN).exists()
    ) or not user.is_active or user.is_access_expired:
        raise PermissionDenied("Permanent deletion is reserved for Super Admin.")
    raise PermissionDenied(
        "Permanent deletion is disabled until an approved retention schedule exists."
    )


def access_required(resource, action, record_getter=None):
    """Guard a function view and optionally authorize its resolved record."""
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            record = record_getter(request, *args, **kwargs) if record_getter else None
            require_access(request.user, resource, action, record)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


class AccessRequiredMixin:
    """Scope a model view's queryset and reject unauthorized single-record access."""

    access_resource = None
    access_action = RoleGrant.Action.READ

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.access_resource is None:
            raise PermissionDenied("A resource permission must be configured.")
        return accessible_queryset(
            self.request.user,
            self.access_resource,
            self.access_action,
            queryset,
        )

    def get_object(self, queryset=None):
        if self.access_resource is None:
            raise PermissionDenied("A resource permission must be configured.")
        instance = super().get_object(queryset)
        if not has_access(self.request.user, self.access_resource, self.access_action, instance):
            raise Http404
        return instance


def accessible_queryset(user, resource, action, queryset):
    """Limit a model queryset to rows visible under role scope and safe shares."""
    if not getattr(user, "is_authenticated", False) or _is_expired(user):
        return queryset.none()
    roles = _role_codes(user)
    if not roles:
        return queryset.none()

    scopes = set(
        RoleGrant.objects.filter(
            role__code__in=roles,
            resource=resource,
            action=action,
        ).values_list("scope", flat=True)
    )
    if RoleGrant.Scope.ALL in scopes:
        visible = queryset
    else:
        visible = queryset.none()
        if RoleGrant.Scope.OWN in scopes:
            ownership = Q()
            for field in ("owner", "user", "created_by"):
                if any(candidate.name == field for candidate in queryset.model._meta.get_fields()):
                    ownership |= Q(**{f"{field}_id": user.pk})
            if ownership:
                visible = visible | queryset.filter(ownership)
        if RoleGrant.Scope.ASSIGNED in scopes or RoleGrant.Scope.LIMITED in scopes:
            if any(
                candidate.name == "assigned_users"
                for candidate in queryset.model._meta.get_fields()
            ):
                visible = visible | queryset.filter(assigned_users=user)
            elif any(candidate.name == "owner" for candidate in queryset.model._meta.get_fields()):
                visible = visible | queryset.filter(owner=user)

    content_type = ContentType.objects.get_for_model(queryset.model, for_concrete_model=False)
    shared_ids = [
        converted_id
        for object_id, actions in RecordShare.objects.filter(
        content_type=content_type,
        user=user,
    ).values_list("object_id", "actions")
        if action in actions
        for converted_id in [_convert_primary_key(queryset, object_id)]
        if converted_id is not None
    ]
    visible = visible | queryset.filter(pk__in=shared_ids)

    if RoleGrant.Scope.AGGREGATE in scopes or RoleGrant.Scope.SYSTEM_HEALTH in scopes:
        # Aggregate/system-health grants do not expose individual model rows.
        if not (RoleGrant.Scope.ALL in scopes or scopes - {RoleGrant.Scope.AGGREGATE, RoleGrant.Scope.SYSTEM_HEALTH}):
            visible = queryset.none()

    model_fields = {field.name for field in queryset.model._meta.get_fields()}
    if "sensitivity" in model_fields:
        if SUPER_ADMIN not in roles:
            visible = visible.exclude(sensitivity="restricted")
        non_system_direct = RoleGrant.objects.filter(
                role__code__in=roles - {SYSTEM_ADMIN, BOARD_MEMBER},
            resource=resource,
            action=action,
            scope__in=(RoleGrant.Scope.ALL, RoleGrant.Scope.ASSIGNED, RoleGrant.Scope.OWN),
        ).exists()
        if (
            SYSTEM_ADMIN in roles
            and not non_system_direct
        ):
            visible = visible.exclude(sensitivity="confidential")
        non_board_direct = RoleGrant.objects.filter(
            role__code__in=roles - {BOARD_MEMBER},
            resource=resource,
            action=action,
            scope__in=(RoleGrant.Scope.ALL, RoleGrant.Scope.ASSIGNED, RoleGrant.Scope.OWN),
        ).exists()
        if (
            BOARD_MEMBER in roles
            and resource == RoleGrant.Resource.CENTRE_CONFIDENTIAL_DOCUMENTS
            and not non_system_direct
        ):
            visible = visible.exclude(sensitivity="confidential")
        if (
            BOARD_MEMBER in roles
            and resource in {RoleGrant.Resource.INCUBATEES, RoleGrant.Resource.CHECKINS_METRICS}
            and not non_board_direct
        ):
            visible = queryset.none()
    return visible.distinct()


def authorized_export_queryset(user, resource, queryset):
    """Use the same row-level read policy for exports as on-screen lists."""
    return accessible_queryset(user, resource, RoleGrant.Action.READ, queryset)


def authorized_task_queryset(user, resource, action, queryset):
    """Scope background-job reads; task callers must supply the initiating user."""
    return accessible_queryset(user, resource, action, queryset)


def authorized_file_download(user, resource, record, file_field, filename=None):
    """Return a private attachment only after checking the record's read grant."""
    from django.http import FileResponse

    require_access(user, resource, RoleGrant.Action.READ, record)
    stream = file_field.open("rb")
    response = FileResponse(
        stream,
        as_attachment=True,
        filename=filename or file_field.name.rsplit("/", 1)[-1],
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def aggregate_queryset(user, resource, queryset, *fields):
    """Return approved non-identifying groupings only, never source records."""
    if not getattr(user, "is_authenticated", False) or _is_expired(user):
        raise PermissionDenied
    roles = _role_codes(user)
    if not RoleGrant.objects.filter(
        role__code__in=roles,
        resource=resource,
        action=RoleGrant.Action.READ,
        scope=RoleGrant.Scope.AGGREGATE,
    ).exists():
        raise PermissionDenied
    allowed_dimensions = {"cohort", "sector", "province", "gender", "youth", "status"}
    if not fields or any(field not in allowed_dimensions for field in fields):
        raise PermissionDenied
    return queryset.values(*fields).annotate(record_count=Count("pk"))


def _convert_primary_key(queryset, value):
    try:
        return queryset.model._meta.pk.to_python(value)
    except (TypeError, ValueError, ValidationError):
        return None


def share_record(actor, user, record, resource, actions):
    """Create a record share only when the actor can update that record."""
    if not user.is_active or user.is_access_expired:
        raise PermissionDenied
    if _security_denies(user, record, resource, RoleGrant.Action.UPDATE, _role_codes(user)):
        raise PermissionDenied
    if not has_access(actor, resource, RoleGrant.Action.UPDATE, record):
        raise PermissionDenied
    if getattr(record, "sensitivity", None) == "restricted":
        raise PermissionDenied
    permitted_actions = {choice for choice, _label in RoleGrant.Action.choices}
    if not actions or not set(actions).issubset(permitted_actions):
        raise PermissionDenied
    if any(not has_access(actor, resource, action, record) for action in actions):
        raise PermissionDenied
    content_type = ContentType.objects.get_for_model(record, for_concrete_model=False)
    share, _created = RecordShare.objects.update_or_create(
        content_type=content_type,
        object_id=str(record.pk),
        user=user,
        defaults={"actions": sorted(set(actions)), "created_by": actor},
    )
    return share
