"""Reading each provider's status feed on a schedule.

The same shape as the monitoring engine and the notification dispatcher: the table is the queue
(`Provider.next_poll_at`), a pass claims what is due with `select_for_update(skip_locked)` so
overlapping passes never read the same provider twice, network calls happen outside any lock,
and one slow or broken provider can't hold up the rest.

Providers someone tracks are read every `PROVIDER_POLL_SECONDS` (5 minutes, matching the
trigger); the rest only every `PROVIDER_IDLE_POLL_SECONDS` (so the browse list still shows a
recent status, without 20 requests every five minutes for nobody). A failing feed backs off.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from django.conf import settings
from django.db import connections, transaction
from django.db.models import Q
from django.utils import timezone

from monitoring import target_validation as tv

from .adapters import ADAPTERS, FeedError
from .ingest import ingest, record_failure
from .models import Provider

logger = logging.getLogger(__name__)

MAX_BYTES = 3 * 1024 * 1024  # a status feed larger than this isn't one we want to hold
LEASE = timedelta(minutes=3)  # how long a claimed provider is left alone if the pass dies
HEADERS = {
    "User-Agent": "RootPulse/1.0 (dependency status monitoring)",
    "Accept": "application/json",
}


@dataclass
class PollSummary:
    claimed: int = 0
    ok: int = 0
    failed: int = 0
    new_incidents: int = 0
    alerts: int = 0
    duration_ms: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, **counts: int) -> None:
        with self._lock:
            for name, value in counts.items():
                setattr(self, name, getattr(self, name) + value)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def fetch_json(client: httpx.Client, url: str) -> Any:
    """GET one document. Errors name the problem but never the address of a secret (there are
    none: every feed is public)."""
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise FeedError(f"HTTP {response.status_code}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise FeedError("The feed is too large.")
    except httpx.TimeoutException as exc:
        raise FeedError("Timed out") from exc
    except httpx.HTTPError as exc:
        raise FeedError("Couldn't connect") from exc
    try:
        return json.loads(bytes(body))
    except ValueError as exc:
        raise FeedError("Not JSON") from exc


def _vet_request(request: httpx.Request) -> None:
    """Runs before every request, including each redirect hop: the host must resolve to public
    addresses only. The catalogue's own hosts are trusted, but a redirect is chosen by whoever
    answered, so a hop to an internal address is refused rather than fetched."""
    try:
        tv.resolve_public_ips(request.url.host)
    except (tv.TargetRejected, tv.ResolutionFailed) as exc:
        raise httpx.RequestError("Refused: not a public address", request=request) from exc


def new_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=settings.PROVIDER_HTTP_TIMEOUT,
        follow_redirects=True,
        max_redirects=3,
        headers=HEADERS,
        event_hooks={"request": [_vet_request]},
    )


def read_feed(provider: Provider, *, client: httpx.Client | None = None):
    """Fetch and parse a provider's feed. Raises FeedError."""
    adapter = ADAPTERS.get(provider.adapter)
    if adapter is None:
        raise FeedError("Unknown feed format")
    own = client is None
    client = client or new_client()
    try:
        docs: dict[str, Any] = {}
        for name, url in adapter.paths(provider.feed_url.rstrip("/")).items():
            try:
                docs[name] = fetch_json(client, url)
            except FeedError:
                if name in adapter.required:
                    raise  # without this document there is nothing to report
                # Optional documents (a history) only add detail.
        return adapter.parse(docs)
    finally:
        if own:
            client.close()


def _interval(provider: Provider) -> timedelta:
    """When to read this provider next: soon if someone tracks it, and backing off on failure."""
    tracked = provider.subscriptions.exists()
    base = settings.PROVIDER_POLL_SECONDS if tracked else settings.PROVIDER_IDLE_POLL_SECONDS
    if provider.consecutive_failures:
        base = min(3600, settings.PROVIDER_POLL_SECONDS * 2**provider.consecutive_failures)
    return timedelta(seconds=base)


def claim_due(now: datetime, limit: int) -> list[int]:
    with transaction.atomic():
        due = list(
            Provider.objects.select_for_update(skip_locked=True)
            .filter(enabled=True)
            .filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
            .order_by("next_poll_at")[:limit]
        )
        for provider in due:
            provider.next_poll_at = now + LEASE
            provider.save(update_fields=["next_poll_at"])
    return [p.pk for p in due]


def poll_provider(
    provider_id: int, *, client: httpx.Client | None = None, now: datetime | None = None
):
    """Read one provider and store the result. Returns (ok, IngestSummary | None)."""
    provider = Provider.objects.get(pk=provider_id)
    now = now or timezone.now()
    try:
        result = read_feed(provider, client=client)
    except FeedError as exc:
        record_failure(provider, str(exc), now=now)
        provider.next_poll_at = now + _interval(provider)
        provider.save(update_fields=["next_poll_at"])
        return False, None
    except Exception:  # noqa: BLE001 - a bug in one adapter mustn't stop the other providers
        logger.exception("reading %s failed", provider.slug)
        record_failure(provider, "Unexpected error", now=now)
        provider.next_poll_at = now + _interval(provider)
        provider.save(update_fields=["next_poll_at"])
        return False, None
    summary = ingest(provider, result, now=now)
    provider.refresh_from_db()
    provider.next_poll_at = now + _interval(provider)
    provider.save(update_fields=["next_poll_at"])
    return True, summary


def run_provider_polls_once(
    *,
    now: datetime | None = None,
    workers: int | None = None,
    limit: int | None = None,
    time_budget: float | None = None,
    client: httpx.Client | None = None,
) -> PollSummary:
    began = time.monotonic()
    now = now or timezone.now()
    budget = time_budget if time_budget is not None else settings.PROVIDER_PASS_BUDGET_SECONDS
    deadline = began + budget
    summary = PollSummary()
    ids = claim_due(now, limit or settings.PROVIDER_BATCH_SIZE)
    summary.claimed = len(ids)

    def task(provider_id: int, in_worker_thread: bool) -> None:
        try:
            if time.monotonic() > deadline:  # out of time: hand it back for the next pass
                Provider.objects.filter(pk=provider_id).update(next_poll_at=now)
                return
            ok, detail = poll_provider(provider_id, client=client, now=now)
            if ok and detail is not None:
                summary.add(ok=1, new_incidents=detail.created, alerts=detail.alerts)
            else:
                summary.add(failed=1)
        except Exception:  # noqa: BLE001
            logger.exception("polling provider %s failed", provider_id)
            summary.add(failed=1)
        finally:
            if in_worker_thread:
                connections.close_all()

    pool_size = workers or settings.PROVIDER_WORKERS
    if pool_size <= 1 or len(ids) <= 1:
        for provider_id in ids:
            task(provider_id, False)
    else:
        with ThreadPoolExecutor(max_workers=min(pool_size, len(ids))) as pool:
            list(pool.map(lambda i: task(i, True), ids))
    summary.duration_ms = round((time.monotonic() - began) * 1000)
    return summary
