import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models

KEY_PREFIX = "rp_"
# How much of the key is kept in the clear, to tell keys apart in a list ("rp_Ab3dE9fG").
VISIBLE_CHARS = len(KEY_PREFIX) + 8


def hash_key(raw: str) -> str:
    """SHA-256, not a slow password hash: a key is 256 random bits, so there is nothing to guess
    and nothing for a slow hash to protect; and it is looked up on every request, so it has to
    be fast and indexable. A leaked table of these hashes can't be turned back into keys."""
    return hashlib.sha256(raw.encode()).hexdigest()


def new_raw_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


class ApiKey(models.Model):
    """A credential for scripts and CI: the dashboard's own API, with a different sign-in."""

    class Scope(models.TextChoices):
        READ = "read", "Read only"
        FULL = "full", "Full access"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_keys"
    )
    name = models.CharField(max_length=80)
    # Only the hash is stored. The raw key is shown once, when it is made.
    key_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=VISIBLE_CHARS)
    scope = models.CharField(max_length=8, choices=Scope.choices, default=Scope.READ)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @classmethod
    def issue(cls, user, name: str, scope: str) -> tuple["ApiKey", str]:
        """Make a key. Returns it and the raw secret, which exists nowhere else."""
        raw = new_raw_key()
        key = cls.objects.create(
            user=user,
            name=name,
            scope=scope,
            key_hash=hash_key(raw),
            prefix=raw[:VISIBLE_CHARS],
        )
        return key, raw
