"""Outbound HTTP for alert channels.

Slack, Discord and Telegram talk to fixed, well-known hosts. A **generic webhook** is
different: its URL is typed in by the user, so RootPulse would otherwise be a tool for
making requests to anywhere, including its own internal network. `safe_post` applies the
same rules as the monitoring engine (docs/plan/04-security.md section 9): resolve the
name, judge the addresses, connect to the vetted IP (never re-resolving), and never
follow a redirect.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings

from monitoring import target_validation as tv

MAX_RESPONSE_BYTES = 64 * 1024


class DeliveryError(Exception):
    """A send that failed. `retryable` decides whether the dispatcher tries again."""

    def __init__(self, message: str, *, retryable: bool, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


def classify_status(status: int, host: str, retry_after: str | None = None) -> DeliveryError:
    """A non-2xx reply. Throttling and server trouble are worth retrying; the rest isn't."""
    wait = int(retry_after) if retry_after and retry_after.isdigit() else None
    if status == 429:
        return DeliveryError(f"{host} is rate limiting us (429)", retryable=True, retry_after=wait)
    if status >= 500:
        return DeliveryError(f"{host} had a server error ({status})", retryable=True)
    if status in (401, 403):
        return DeliveryError(
            f"{host} refused the request ({status}); check the credentials", retryable=False
        )
    if status in (404, 410):
        return DeliveryError(
            f"{host} says the destination no longer exists ({status})", retryable=False
        )
    return DeliveryError(f"{host} rejected the request ({status})", retryable=False)


def _transport_error(exc: httpx.HTTPError, host: str) -> DeliveryError:
    # Never put str(exc) in the message: httpx includes URLs, and a webhook URL is a secret.
    if isinstance(exc, httpx.TimeoutException):
        return DeliveryError(f"Timed out talking to {host}", retryable=True)
    return DeliveryError(f"Couldn't reach {host}", retryable=True)


def post_json(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    """POST to a trusted, fixed host (Slack, Discord, Telegram). No redirects."""
    host = urlsplit(url).hostname or "the server"
    try:
        with httpx.Client(
            transport=transport, timeout=settings.NOTIFY_HTTP_TIMEOUT, follow_redirects=False
        ) as client:
            return client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise _transport_error(exc, host) from exc


def safe_post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    resolve=tv.resolve_public_ips,
    transport: httpx.BaseTransport | None = None,
) -> tuple[int, str]:
    """POST to a user-supplied URL, safely. Returns (status, first bytes of the reply)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host or parts.username is not None:
        raise DeliveryError("The webhook URL isn't a valid http(s) URL", retryable=False)
    try:
        ips = resolve(host)
    except tv.TargetRejected as exc:
        raise DeliveryError(f"The webhook URL isn't allowed: {exc}", retryable=False) from exc
    except tv.ResolutionFailed as exc:
        raise DeliveryError(f"Couldn't look up {host}", retryable=True) from exc

    default_port = 443 if parts.scheme == "https" else 80
    shown_host = f"[{host}]" if ":" in host else host
    host_header = shown_host if parts.port in (None, default_port) else f"{shown_host}:{parts.port}"
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    extensions: dict[str, Any] = {"sni_hostname": host} if parts.scheme == "https" else {}

    last: DeliveryError | None = None
    for ip in ips:
        ip_text = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
        port = f":{parts.port}" if parts.port else ""
        target = f"{parts.scheme}://{ip_text}{port}{path}"
        try:
            with httpx.Client(
                transport=transport, timeout=settings.NOTIFY_HTTP_TIMEOUT, follow_redirects=False
            ) as client:
                request = client.build_request(
                    "POST",
                    target,
                    content=body,
                    headers={**headers, "Host": host_header},
                    extensions=extensions,
                )
                response = client.send(request, stream=True)
                try:
                    text = b"".join(response.iter_bytes())[:MAX_RESPONSE_BYTES].decode(
                        "utf-8", errors="replace"
                    )
                finally:
                    response.close()
        except httpx.HTTPError as exc:
            last = _transport_error(exc, host)
            continue
        return response.status_code, text
    raise last or DeliveryError(f"Couldn't reach {host}", retryable=True)
