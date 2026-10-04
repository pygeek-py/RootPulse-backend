"""HMAC request signing for the internal surface: the scheduler trigger, and the
calls between this API and the Cloudflare Worker probers.

Header format:  X-RootPulse-Signature: t=<unix seconds>,v1=<hex hmac-sha256>
The MAC covers "<t>." followed by the exact body bytes, and the timestamp must
be recent, so a captured request can't be replayed later.
"""

from __future__ import annotations

import hashlib
import hmac
import time

HEADER = "X-RootPulse-Signature"
DEFAULT_TOLERANCE_SECONDS = 300


def _mac(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes = b"", now: float | None = None) -> str:
    timestamp = int(now if now is not None else time.time())
    return f"t={timestamp},v1={_mac(secret, timestamp, body)}"


def verify(
    secret: str,
    header: str | None,
    body: bytes = b"",
    *,
    now: float | None = None,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """True only for a well-formed, correctly signed, fresh header. Never raises."""
    if not secret or not header:
        return False
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        provided = parts["v1"]
    except (ValueError, KeyError):
        return False
    current = now if now is not None else time.time()
    if abs(current - timestamp) > tolerance:
        return False
    return hmac.compare_digest(provided, _mac(secret, timestamp, body))
