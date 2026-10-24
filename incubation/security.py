import base64
import hashlib
import hmac

from cryptography.fernet import Fernet
from django.conf import settings


def _fernet():
    key = settings.MFA_ENCRYPTION_KEY.encode()
    if len(key) != 44:
        key = base64.urlsafe_b64encode(hashlib.sha256(key).digest())
    return Fernet(key)


def encrypt_id_number(value):
    return _fernet().encrypt(value.strip().encode()).decode() if value.strip() else ""


def decrypt_id_number(value):
    return _fernet().decrypt(value.encode()).decode() if value else ""


def id_number_fingerprint(value):
    normalized = "".join(value.split()).upper()
    if not normalized:
        return ""
    return hmac.new(
        settings.SECRET_KEY.encode(),
        normalized.encode(),
        hashlib.sha256,
    ).hexdigest()
