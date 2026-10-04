"""Runs one check for a monitor, by type. Pure dispatch: no database writes, so it
can be tested (and reused for re-checks) in isolation."""

from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from .checks import result as r
from .checks.dns_check import run_dns
from .checks.domain_check import run_domain
from .checks.heartbeat_check import evaluate_heartbeat
from .checks.http_check import HttpChecker
from .checks.ssl_check import run_ssl
from .checks.tcp_check import run_tcp
from .models import Monitor

logger = logging.getLogger(__name__)

# What a run hands back: the result, and any engine state to persist (e.g. DNS records).
Outcome = tuple[r.CheckResult, dict | None]


def run_primary(monitor: Monitor, *, now: datetime | None = None) -> Outcome:
    config = monitor.config or {}
    kind = monitor.type
    try:
        if kind == Monitor.Type.HTTP:
            return HttpChecker().run(monitor.target, config), None
        if kind == Monitor.Type.KEYWORD:
            return HttpChecker().run(monitor.target, config, keyword=config["keyword"]), None
        if kind in (Monitor.Type.PING, Monitor.Type.PORT):
            result = run_tcp(monitor.target, config["port"], settings.CHECK_TCP_TIMEOUT)
            return result, None
        if kind == Monitor.Type.SSL:
            result = run_ssl(
                monitor.target,
                config.get("port", 443),
                config.get("warn_days", 14),
                settings.CHECK_TCP_TIMEOUT,
            )
            return result, None
        if kind == Monitor.Type.DOMAIN:
            return run_domain(monitor.target, config.get("warn_days", 30)), None
        if kind == Monitor.Type.DNS:
            previous = (monitor.state or {}).get("dns")
            result, snapshot = run_dns(monitor.target, config.get("record_types", ["A"]), previous)
            return result, ({"dns": snapshot} if snapshot is not None else None)
        if kind == Monitor.Type.CRON:
            result = evaluate_heartbeat(
                created_at=monitor.created_at,
                last_ping=monitor.last_heartbeat_at,
                interval_seconds=monitor.interval_seconds,
                grace_seconds=config.get("grace_period_seconds", 300),
                now=now or timezone.now(),
            )
            return result, None
    except Exception as exc:  # noqa: BLE001 - one bad check must never abort the pass
        logger.exception("check crashed for monitor %s", monitor.id)
        return r.CheckResult.inconclusive(r.INTERNAL_ERROR, error=type(exc).__name__), None
    return r.CheckResult.inconclusive(r.INTERNAL_ERROR, error="unknown_type"), None
