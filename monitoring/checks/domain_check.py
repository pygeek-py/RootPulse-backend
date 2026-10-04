"""Domain expiration monitor via RDAP (the JSON successor to WHOIS).

IANA publishes which RDAP server handles each TLD. Lookups that can't be
answered (a TLD without RDAP, a slow registry, an unparseable reply) are
*inconclusive*, never "down": a missing answer must not look like an expiry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
from django.core.cache import cache

from . import result as r

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_CACHE_KEY = "rdap-bootstrap-v1"
BOOTSTRAP_TTL = 24 * 3600


def _rdap_base(client: httpx.Client, tld: str) -> str | None:
    services = cache.get(BOOTSTRAP_CACHE_KEY)
    if services is None:
        response = client.get(BOOTSTRAP_URL)
        response.raise_for_status()
        services = response.json().get("services", [])
        cache.set(BOOTSTRAP_CACHE_KEY, services, BOOTSTRAP_TTL)
    for tlds, urls in services:
        if tld in tlds and urls:
            return next((u for u in urls if u.startswith("https://")), urls[0]).rstrip("/")
    return None


def _expiry(document: dict[str, Any]) -> datetime | None:
    for event in document.get("events", []):
        if event.get("eventAction") == "expiration" and event.get("eventDate"):
            text = str(event["eventDate"]).replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def run_domain(
    domain: str,
    warn_days: int,
    *,
    transport: httpx.BaseTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    clock: Callable[[], float] = time.perf_counter,
) -> r.CheckResult:
    started = clock()

    def unavailable(reason: str) -> r.CheckResult:
        return r.CheckResult.inconclusive(r.LOOKUP_UNAVAILABLE, reason=reason)

    # Fixed, trusted hosts only (IANA and the registry it names); never follows redirects.
    with httpx.Client(
        transport=transport,
        timeout=10.0,
        follow_redirects=False,
        headers={"Accept": "application/rdap+json"},
    ) as client:
        try:
            base = _rdap_base(client, domain.rsplit(".", 1)[-1])
            if base is None:
                return unavailable("tld_has_no_rdap")
            response = client.get(f"{base}/domain/{domain}")
        except httpx.HTTPError:
            return unavailable("rdap_unreachable")
    if response.status_code != 200:
        return unavailable(f"rdap_status_{response.status_code}")
    try:
        expires = _expiry(response.json())
    except ValueError:
        return unavailable("rdap_unreadable")
    if expires is None:
        return unavailable("no_expiry_in_rdap")

    days = (expires - now()).days
    ok = days >= warn_days
    return r.CheckResult(
        success=ok,
        status_detail=r.OK if ok else r.DOMAIN_EXPIRING,
        duration_ms=r.elapsed_ms(started, clock()),
        detail={"days_remaining": days, "expires": expires.isoformat()},
    )
