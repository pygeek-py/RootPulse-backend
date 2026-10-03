import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user model, in place from commit 1 per docs/plan/01-tech-stack.md.

    Email is the identifier; `username` is kept (inherited from AbstractUser)
    but unused. Sign-in is passwordless (email link/code or GitHub), so every
    user has an unusable password; only Django admin superusers set one.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    github_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    created_at = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self) -> str:
        return self.email


class EmailChallenge(models.Model):
    """A pending passwordless sign-in/sign-up: one emailed link + 6-digit code.

    Only hashes are stored, so a database leak can't be replayed. The link
    token is 256 random bits; the code is guessable, so it dies after
    `EMAIL_CODE_MAX_ATTEMPTS` wrong tries. Either one consumes the challenge.
    """

    SIGNUP = "signup"
    LOGIN = "login"
    PURPOSES = [(SIGNUP, "Sign up"), (LOGIN, "Log in")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    purpose = models.CharField(max_length=8, choices=PURPOSES)
    link_hash = models.CharField(max_length=64, unique=True)
    code_hash = models.CharField(max_length=64)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["email", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.purpose} challenge for {self.email}"


class AuditLog(models.Model):
    """Security-relevant action trail — see docs/plan/04-security.md #15."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Nullable: a failed login for an unknown email has no user to point at,
    # and SET_NULL keeps the trail after an account is deleted.
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.action} by {self.user_id} at {self.created_at}"
