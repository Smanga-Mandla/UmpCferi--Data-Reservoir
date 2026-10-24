from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateMediaStorage(FileSystemStorage):
    """Store uploads outside static/public paths and never produce public URLs."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("location", settings.PRIVATE_MEDIA_ROOT)
        kwargs.setdefault("base_url", None)
        super().__init__(*args, **kwargs)

    def url(self, name):
        raise ValueError("Private files must be served through an authorized view.")
