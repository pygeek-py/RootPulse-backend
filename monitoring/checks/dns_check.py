"""DNS monitor: resolve the chosen record types and alert when they change.

The first check records a baseline (and counts as up). After that, any change
from the last-seen records fails that one check and becomes the new baseline,
so a change alerts once and then reads as "recovered", rather than the monitor
staying down until someone resets it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import dns.exception
import dns.resolver

from . import result as r

Lookup = Callable[[str, str], list[str]]


class NoSuchDomain(Exception):
    pass


class LookupFailed(Exception):
    """The resolver couldn't answer (timeout, SERVFAIL...): not evidence the records changed."""


def lookup_records(domain: str, rtype: str, lifetime: float = 5.0) -> list[str]:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = lifetime
    try:
        answer = resolver.resolve(domain, rtype)
    except dns.resolver.NXDOMAIN as exc:
        raise NoSuchDomain(domain) from exc
    except dns.resolver.NoAnswer:
        return []
    except dns.exception.DNSException as exc:
        raise LookupFailed(str(exc)) from exc
    return sorted(rdata.to_text() for rdata in answer)


def run_dns(
    domain: str,
    record_types: list[str],
    previous: dict[str, list[str]] | None,
    *,
    lookup: Lookup = lookup_records,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[r.CheckResult, dict[str, list[str]] | None]:
    """Returns (result, snapshot to store). A None snapshot means "keep what is stored"."""
    started = clock()
    current: dict[str, list[str]] = {}
    try:
        for rtype in record_types:
            try:
                current[rtype] = lookup(domain, rtype)
            except NoSuchDomain:
                current[rtype] = []
    except LookupFailed as exc:
        return r.CheckResult.inconclusive(r.LOOKUP_UNAVAILABLE, reason=str(exc)[:200]), None
    elapsed = r.elapsed_ms(started, clock())

    if previous is None:
        result = r.CheckResult(
            success=True,
            status_detail=r.DNS_BASELINE,
            duration_ms=elapsed,
            dns_ms=elapsed,
            detail={"records": current},
        )
        return result, current

    changed: dict[str, Any] = {}
    for rtype in record_types:
        before, after = previous.get(rtype, []), current.get(rtype, [])
        if before != after:
            changed[rtype] = {"before": before, "after": after}
    if not changed:
        result = r.CheckResult(
            success=True,
            status_detail=r.OK,
            duration_ms=elapsed,
            dns_ms=elapsed,
            detail={"records": current},
        )
        return result, current
    result = r.CheckResult(
        success=False,
        status_detail=r.DNS_CHANGED,
        duration_ms=elapsed,
        dns_ms=elapsed,
        detail={"changed": changed},
    )
    return result, current
