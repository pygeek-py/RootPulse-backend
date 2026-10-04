"""Create the key pair web push needs. Run once; put both values in your environment."""

import base64

from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate() -> tuple[str, str]:
    """(private key for the server, public key for browsers)"""
    vapid = Vapid()
    vapid.generate_keys()
    private = b64url(
        vapid.private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public = b64url(
        vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    return private, public


class Command(BaseCommand):
    help = "Generate a VAPID key pair for web push notifications."

    def handle(self, *args, **options):
        private, public = generate()
        self.stdout.write("Add these to your environment (.env locally, Render in production):\n")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private}")
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}")
        self.stdout.write(
            "\nKeep the private key secret. Changing it later invalidates every saved browser."
        )
