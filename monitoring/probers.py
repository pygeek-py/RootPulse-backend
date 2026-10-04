"""Client for the regional probers (Cloudflare Workers).

A failure only counts when at least two regions agree, which filters out the
blips a single network path produces (docs/plan/03-monitoring-engine.md section 8).
Each prober is addressed as `name=https://url` in PROBER_URLS. Requests and
replies are both HMAC-signed with PROBER_SHARED_SECRET.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings

from . import signing
from .checks.result import CheckResult

logger = logging.getLogger(__name__)

PROBE_TYPES = ("http", "keyword", "ping", "port")


@dataclass(frozen=True)
class Prober:
    name: str
    url: str


def configured_probers() -> list[Prober]:
    """Parse PROBER_URLS ("frankfurt=https://a.workers.dev,tokyo=https://b.workers.dev")."""
    if not settings.PROBER_SHARED_SECRET:
        return []
    probers = []
    for item in settings.PROBER_URLS.split(","):
        name, _, url = item.strip().partition("=")
        if name and url.startswith(("https://", "http://")):
            probers.append(Prober(name.strip(), url.strip().rstrip("/")))
    return probers


def probe_one(
    prober: Prober,
    monitor_type: str,
    target: str,
    config: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
) -> CheckResult | None:
    """Ask one prober to run the check now. None means it abstained (unreachable,
    refused, or a reply that failed verification), so it neither confirms nor denies."""
    secret = settings.PROBER_SHARED_SECRET
    body = json.dumps({"type": monitor_type, "target": target, "config": config}).encode()
    timeout = float(config.get("timeout_seconds", settings.CHECK_TCP_TIMEOUT)) + 5.0
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(
                f"{prober.url}/probe",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    signing.HEADER: signing.sign(secret, body),
                },
            )
        if response.status_code != 200:
            logger.warning("Prober %s answered %s", prober.name, response.status_code)
            return None
        if not signing.verify(secret, response.headers.get(signing.HEADER), response.content):
            logger.warning("Prober %s sent a reply with a bad signature", prober.name)
            return None
        payload = response.json()
        return CheckResult.from_dict(payload["result"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        logger.warning("Prober %s unreachable", prober.name, exc_info=True)
        return None


def probe_all(
    probers: list[Prober],
    monitor_type: str,
    target: str,
    config: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[tuple[Prober, CheckResult | None]]:
    if not probers:
        return []
    with ThreadPoolExecutor(max_workers=len(probers)) as pool:
        futures = [
            pool.submit(probe_one, p, monitor_type, target, config, transport=transport)
            for p in probers
        ]
        return [(p, f.result()) for p, f in zip(probers, futures, strict=True)]
