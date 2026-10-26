from django.conf import settings
from django.contrib.auth.models import AbstractUser, Permission
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.exceptions import PermissionDenied
from django.db import models
from django.utils import timezone
from accounts.validators import validate_gmail_email

from accounts.managers import UserManager


class Role(models.Model):
    class Code(models.TextChoices):
        SUPER_ADMIN = "super_admin", "Super Admin"
        SYSTEM_ADMIN = "system_admin", "System Admin"
        ADMIN = "admin", "Admin"
        BOARD_MEMBER = "board_member", "Board Member"
        CFERI_STAFF = "cferi_staff", "CFERI Staff"
        FREELANCER = "freelancer", "Freelancer"
        EXTERNAL_GUEST = "external_guest", "External Guest"
        INCUBATEE = "incubatee", "Incubatee"

    code = models.CharField(max_length=32, choices=Code.choices, unique=True)
    permissions = models.ManyToManyField(
        Permission,
        blank=True,
        related_name="cferi_roles",
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.get_code_display()


class RoleGrant(models.Model):
    class Resource(models.TextChoices):
        USERS = "users", "Users and roles"
        CENTRE_CONFIDENTIAL_DOCUMENTS = "centre_confidential_documents", "Centre documents (confidential)"
        CENTRE_GENERAL_DOCUMENTS = "centre_general_documents", "Centre documents (general)"
        PARTNERS_MOUS = "partners_mous", "Partners and MOUs"
        INCUBATEES = "incubatees", "Incubatees"
        CHECKINS_METRICS = "checkins_metrics", "Check-ins and metrics"
        STAKEHOLDERS = "stakeholders", "Stakeholder directory"
        INTERACTIONS = "interactions", "Interactions log"
        REPORTS = "reports", "Reports and dashboards"
        AUDIT_LOG = "audit_log", "Audit log"

    class Action(models.TextChoices):
        CREATE = "create", "Create"
        READ = "read", "Read"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"

    class Scope(models.TextChoices):
        ALL = "all", "All records"
        ASSIGNED = "assigned", "Assigned records"
        OWN = "own", "Own records"
        AGGREGATE = "aggregate", "Aggregated data only"
        SYSTEM_HEALTH = "system_health", "System health only"
        LIMITED = "limited", "Limited/explicitly shared"

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="grants")
    resource = models.CharField(max_length=40, choices=Resource.choices)
    action = models.CharField(max_length=8, choices=Action.choices)
    scope = models.CharField(max_length=16, choices=Scope.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("role", "resource", "action", "scope"),
                name="unique_role_resource_action_scope",
            ),
        ]
        ordering = ("role__code", "resource", "action", "scope")

    def __str__(self):
        return f"{self.role}: {self.get_action_display()} {self.get_resource_display()} ({self.scope})"


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True, validators=[validate_gmail_email])
    roles = models.ManyToManyField(Role, blank=True, related_name="users")
    access_expires_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        self.email = self.__class__.objects.normalize_email(self.email.strip()).lower()
        validate_gmail_email(self.email)
        if (
            self.pk
            and self.access_expires_at is None
            and self.roles.filter(
                code__in=(Role.Code.FREELANCER, Role.Code.EXTERNAL_GUEST)
            ).exists()
        ):
            raise ValidationError(
                "Freelancer and External Guest accounts must have an access expiry date."
            )
        super().save(*args, **kwargs)

    @property
    def has_mandatory_mfa_role(self):
        return self.is_superuser or self.roles.filter(code=Role.Code.SUPER_ADMIN).exists()

    @property
    def requires_access_expiry(self):
        return self.roles.filter(
            code__in=(Role.Code.FREELANCER, Role.Code.EXTERNAL_GUEST)
        ).exists()

    @property
    def is_access_expired(self):
        return self.access_expires_at is not None and self.access_expires_at <= timezone.now()


class LoginThrottle(models.Model):
    email = models.EmailField(unique=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class TOTPDevice(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="totp_devices")
    name = models.CharField(max_length=100, default="Authenticator")
    secret_encrypted = models.TextField()
    confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)


class RecordShare(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.CharField(max_length=64)
    content_object = GenericForeignKey("content_type", "object_id")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="record_shares")
    actions = models.JSONField(default=list)
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_record_shares",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("content_type", "object_id", "user"),
                name="unique_record_share_user",
            ),
        ]


class SoftDeleteQuerySet(models.QuerySet):
    def delete(self, *args, **kwargs):
        raise PermissionDenied(
            "Bulk deletion is disabled; authorize and soft-delete each record explicitly."
        )


class ActiveRecordManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class AccessControlledModel(models.Model):
    access_resource = None

    class Sensitivity(models.TextChoices):
        PUBLIC = "public", "Public"
        INTERNAL = "internal", "Internal"
        CONFIDENTIAL = "confidential", "Confidential"
        RESTRICTED = "restricted", "Restricted"

    sensitivity = models.CharField(
        max_length=16,
        choices=Sensitivity.choices,
        default=Sensitivity.INTERNAL,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_owned",
    )
    assigned_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="%(app_label)s_%(class)s_assigned",
    )
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_deleted",
    )

    objects = ActiveRecordManager.from_queryset(SoftDeleteQuerySet)()
    all_objects = models.Manager.from_queryset(SoftDeleteQuerySet)()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False, *, user=None, permanent=False):
        if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
            raise PermissionDenied("An active, authenticated user must authorize deletion.")
        if getattr(user, "is_access_expired", False):
            raise PermissionDenied("Expired accounts cannot delete records.")
        if self.access_resource is None:
            raise PermissionDenied("A resource permission must be configured before deletion.")
        from accounts.access import require_access

        require_access(user, self.access_resource, RoleGrant.Action.DELETE, self)
        if permanent:
            raise PermissionDenied(
                "Permanent deletion is disabled until an approved retention schedule exists."
            )
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = user if getattr(user, "is_authenticated", False) else None
        self.save(update_fields=("is_deleted", "deleted_at", "deleted_by"))
        return (1, {self._meta.label: 1})

    def restore(self, user, resource):
        from datetime import timedelta

        from django.core.exceptions import PermissionDenied

        from accounts.access import require_access

        if not self.is_deleted:
            return False
        if self.deleted_at is None or self.deleted_at < timezone.now() - timedelta(days=90):
            raise PermissionDenied("The 90-day recovery period has expired.")
        require_access(user, resource, RoleGrant.Action.UPDATE, self)
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=("is_deleted", "deleted_at", "deleted_by"))
        return True
