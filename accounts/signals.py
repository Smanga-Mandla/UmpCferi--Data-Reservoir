from django.core.exceptions import ValidationError
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from accounts.models import Role, User


@receiver(m2m_changed, sender=User.roles.through)
def require_expiry_for_temporary_roles(sender, instance, action, pk_set, **kwargs):
    if action != "pre_add" or not pk_set:
        return
    temporary_role_exists = Role.objects.filter(
        pk__in=pk_set,
        code__in=(Role.Code.FREELANCER, Role.Code.EXTERNAL_GUEST),
    ).exists()
    if temporary_role_exists and instance.access_expires_at is None:
        raise ValidationError(
            "Freelancer and External Guest accounts must have an access expiry date."
        )
