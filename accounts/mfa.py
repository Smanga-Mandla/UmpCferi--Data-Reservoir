import base64
import hashlib
import hmac
import time

import pyotp
from cryptography.fernet import Fernet
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import TOTPDevice

def _fernet():
    raw_key = settings.MFA_ENCRYPTION_KEY.encode()
    if len(raw_key) != 44:
        raw_key = base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest())
    return Fernet(raw_key)


def encrypt_totp_secret(secret):
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_totp_secret(encrypted_secret):
    return _fernet().decrypt(encrypted_secret.encode()).decode()


def verify_totp(device, token):
    if not token or len(token) != 6 or not token.isdigit():
        return False
    with transaction.atomic():
        locked_device = TOTPDevice.objects.select_for_update().get(pk=device.pk)
        totp = pyotp.TOTP(decrypt_totp_secret(locked_device.secret_encrypted))
        current_step = int(time.time() // totp.interval)
        last_used_step = (
            int(locked_device.last_used_at.timestamp() // totp.interval)
            if locked_device.last_used_at
            else -1
        )
        valid = any(
            step > last_used_step
            and hmac.compare_digest(token, totp.at(step * totp.interval))
            for step in range(current_step - 1, current_step + 2)
        )
        if valid:
            locked_device.last_used_at = timezone.now()
            locked_device.save(update_fields=("last_used_at",))
            device.last_used_at = locked_device.last_used_at
        return valid
