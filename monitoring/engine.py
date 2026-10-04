"""The scheduler: one pass claims due monitors from Postgres, checks them, and returns.

Render's free tier has no always-on worker, so this is a *single pass* (not a
loop) triggered every five minutes by GitHub Actions through a signed endpoint
(docs/plan/03-monitoring-engine.md). The `monitors` table is the queue, and
`select_for_update(skip_locked=True)` guarantees two overlapping passes never
claim the same monitor.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone

from . import hooks, probers
from .checks import result as r
from .checks.heartbeat_check import deadline_for
from .models import Check, MaintenanceWindow, Monitor
from .runner import Outcome, run_primary

logger = logging.getLogger(__name__)

# Types where several regions can meaningfully disagree about reachability.
MULTI_REGION = {Monitor.Type.HTTP, Monitor.Type.KEYWORD, Monitor.Type.PING, Monitor.Type.PORT}
# A failure here is deterministic (or a recheck would see our own updated state).
CONFIRMED_IMMEDIATELY = {Monitor.Type.DOMAIN, Monitor.Type.DNS, Monitor.Type.CRON}
# Everything else (SSL) is re-checked from the same region after a short delay.

Runner = Callable[[Monitor], Outcome]


@dataclass
class PassSummary:
    claimed: int = 0
    checked: int = 0
    up: int = 0
    down: int = 0
    blips: int = 0
    inconclusive: int = 0
    skipped_maintenance: int = 0
    released: int = 0
    errors: int = 0
    duration_ms: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def bump(self, name: str) -> None:
        """Increment a counter. Worker threads share one summary, and `+=` isn't atomic."""
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


# --- claiming -------------------------------------------------------------


def claim_due(now: datetime, batch_size: int) -> tuple[dict, int]:
    """Atomically claim up to `batch_size` due monitors.

    Each claimed monitor's `next_check_at` is pushed to now + its interval
    *inside the lock*, so a concurrent or retried pass sees it as not due. The
    network calls then happen outside the transaction, never holding a lock.

    Returns ({monitor_id: original next_check_at}, number skipped for maintenance).
    """
    claimed: dict = {}
    skipped = 0
    with transaction.atomic():
        due = list(
            Monitor.objects.select_for_update(skip_locked=True)
            .filter(
                next_check_at__lte=now,
                status__in=[Monitor.Status.PENDING, Monitor.Status.UP, Monitor.Status.DOWN],
            )
            .order_by("next_check_at")[:batch_size]
        )
        windows = _maintenance_ends(now, [m.id for m in due])
        for monitor in due:
            if monitor.id in windows:
                # Failures during planned downtime must not alert: look again once it's over.
                monitor.next_check_at = windows[monitor.id]
                skipped += 1
            else:
                claimed[monitor.id] = monitor.next_check_at
                monitor.next_check_at = now + timedelta(seconds=monitor.interval_seconds)
            monitor.save(update_fields=["next_check_at"])
    return claimed, skipped


def _maintenance_ends(now: datetime, monitor_ids: list) -> dict:
    """{monitor_id: when its current maintenance window ends} for monitors in one now."""
    if not monitor_ids:
        return {}
    ends: dict = {}
    rows = MaintenanceWindow.objects.filter(
        starts_at__lte=now, ends_at__gt=now, monitors__in=monitor_ids
    ).values_list("monitors", "ends_at")
    for monitor_id, ends_at in rows:
        ends[monitor_id] = max(ends_at, ends.get(monitor_id, ends_at))
    return ends


# --- executing one monitor ------------------------------------------------


def _record(
    monitor: Monitor,
    result: r.CheckResult,
    started: datetime,
    region: str,
    confirmation: bool = False,
) -> Check:
    return Check.objects.create(
        monitor=monitor,
        region=region,
        confirmation=confirmation,
        started_at=started,
        duration_ms=result.duration_ms,
        success=result.success,
        status_detail=result.status_detail,
        status_code=result.status_code,
        dns_ms=result.dns_ms,
        tcp_ms=result.tcp_ms,
        tls_ms=result.tls_ms,
        ttfb_ms=result.ttfb_ms,
        detail=result.detail,
    )


class Engine:
    def __init__(
        self,
        *,
        runner: Runner | None = None,
        probe: Callable[..., list] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        # Resolved when the engine is built, so tests can swap the module-level pieces.
        self.runner = runner or run_primary
        self.probe = probe or probers.probe_all
        self.sleep = sleep or time.sleep

    # -- the confirmation protocol (docs/plan/03-monitoring-engine.md section 8) --

    def _confirm(self, monitor: Monitor, primary: r.CheckResult) -> tuple[bool, list]:
        """Is this failure real? Returns (confirmed, [(region, result) extra checks])."""
        kind = monitor.type
        if kind in CONFIRMED_IMMEDIATELY:
            return True, []

        extra: list[tuple[str, r.CheckResult]] = []
        if kind in MULTI_REGION:
            configured = probers.configured_probers()
            if configured:
                answers = self.probe(configured, kind, monitor.target, self._probe_config(monitor))
                responded = [(p, res) for p, res in answers if res is not None]
                extra = [(p.name, res) for p, res in responded]
                if responded:
                    failures = 1 + sum(1 for _, res in responded if res.success is False)
                    # Two regions must agree. (With a single responder, both must fail.)
                    return failures >= 2, extra

        # No prober configured or none answered: re-check from here after a short pause.
        self.sleep(settings.CHECK_RECHECK_DELAY_SECONDS)
        again, _ = self.runner(monitor)
        extra.append((settings.PRIMARY_REGION, again))
        return again.success is False, extra

    @staticmethod
    def _probe_config(monitor: Monitor) -> dict:
        return dict(monitor.config or {})

    # -- one monitor ---------------------------------------------------------

    def execute(self, monitor_id, summary: PassSummary) -> None:
        monitor = Monitor.objects.filter(pk=monitor_id).first()
        if monitor is None or monitor.status == Monitor.Status.PAUSED:
            return  # deleted or paused between claiming and running

        started = timezone.now()
        primary, state_patch = self.runner(monitor)
        region = settings.PRIMARY_REGION
        primary_check = _record(monitor, primary, started, region)
        summary.bump("checked")

        new_status = monitor.status
        if primary.success is True:
            new_status = Monitor.Status.UP
            summary.bump("up")
        elif primary.success is None:
            summary.bump("inconclusive")
        else:
            if monitor.status == Monitor.Status.DOWN:
                new_status = Monitor.Status.DOWN  # already down: just log the check
                summary.bump("down")
            else:
                confirmed, extra = self._confirm(monitor, primary)
                for extra_region, extra_result in extra:
                    _record(monitor, extra_result, started, extra_region, confirmation=True)
                if confirmed:
                    new_status = Monitor.Status.DOWN
                    summary.bump("down")
                else:
                    summary.bump("blips")  # a blip: logged, no action

        self._apply(monitor, primary, primary_check, new_status, state_patch, started)

    def _apply(self, monitor, primary, check, new_status, state_patch, started) -> None:
        """Write the monitor's new state, unless the user changed or paused it meanwhile."""
        fields: dict = {
            "last_check_at": started,
            "last_status_detail": primary.status_detail,
            "status": new_status,
        }
        if primary.duration_ms is not None:
            fields["last_response_ms"] = primary.duration_ms
        if state_patch:
            fields["state"] = {**(monitor.state or {}), **state_patch}
        if monitor.type == Monitor.Type.CRON and primary.success:
            fields["next_check_at"] = deadline_for(
                monitor.created_at,
                monitor.last_heartbeat_at,
                monitor.interval_seconds,
                (monitor.config or {}).get("grace_period_seconds", 300),
            )

        updated = (
            Monitor.objects.filter(pk=monitor.pk, updated_at=monitor.updated_at)
            .exclude(status=Monitor.Status.PAUSED)
            .update(**fields)
        )
        if updated and new_status != monitor.status:
            hooks.status_changed(monitor, monitor.status, new_status, check)


# --- the pass -------------------------------------------------------------


def run_scheduler_once(
    *,
    engine: Engine | None = None,
    batch_size: int | None = None,
    workers: int | None = None,
    time_budget: float | None = None,
    now: datetime | None = None,
) -> PassSummary:
    """One scheduler pass: claim what is due, check it concurrently, return a summary."""
    engine = engine or Engine()
    batch_size = batch_size or settings.CHECK_BATCH_SIZE
    workers = workers or settings.CHECK_WORKERS
    budget = time_budget if time_budget is not None else settings.CHECK_PASS_BUDGET_SECONDS
    began = time.monotonic()
    deadline = began + budget
    now = now or timezone.now()

    summary = PassSummary()
    claimed, skipped = claim_due(now, batch_size)
    summary.claimed = len(claimed)
    summary.skipped_maintenance = skipped

    def task(monitor_id, *, in_worker_thread):
        try:
            if time.monotonic() > deadline:
                # Out of time (the trigger's HTTP request must finish): hand it back unchecked.
                Monitor.objects.filter(pk=monitor_id).update(next_check_at=claimed[monitor_id])
                summary.bump("released")
                return
            engine.execute(monitor_id, summary)
        except Exception:  # noqa: BLE001 - one monitor must never sink the pass
            logger.exception("executing monitor %s failed", monitor_id)
            summary.bump("errors")
        finally:
            if in_worker_thread:
                # A pool thread owns its own DB connection and must release it. The calling
                # thread's connection belongs to the caller (a request, a test), so leave it.
                connections.close_all()

    ids = list(claimed)
    if workers <= 1 or len(ids) <= 1:
        for monitor_id in ids:
            task(monitor_id, in_worker_thread=False)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(ids))) as pool:
            list(pool.map(lambda mid: task(mid, in_worker_thread=True), ids))

    summary.duration_ms = round((time.monotonic() - began) * 1000)
    return summary
