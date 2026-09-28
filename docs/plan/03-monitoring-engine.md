# Monitoring Engine

This is the core of the product — everything else (dashboard, alerts, status pages) is a view onto what this engine produces. Read alongside `01-tech-stack.md`'s callout on why this is Postgres-native rather than Celery/Redis-based at MVP scale.

## 1. Scheduler

A Django management command, `run_scheduler`, run as an always-on process (Fly.io, restarted by the platform on crash).

```python
# conceptual, not final code
while True:
    with transaction.atomic():
        due = (Monitor.objects
               .select_for_update(skip_locked=True)
               .filter(next_check_at__lte=now(), status__in=["pending", "up", "down"])
               .exclude(paused=True)
               .exclude(in_maintenance_window=True)   # subquery against MaintenanceWindow
               .order_by("next_check_at")[:BATCH_SIZE])
        for monitor in due:
            monitor.next_check_at = now() + monitor.interval_seconds  # claim immediately
            monitor.save(update_fields=["next_check_at"])
    dispatch_to_thread_pool(due)   # outside the transaction — network calls never hold a DB lock
    sleep(POLL_INTERVAL_SECONDS)   # e.g. 1s
```

`skip_locked=True` means a second scheduler process (if ever run for redundancy) never double-claims the same monitor — this is what makes the design safely horizontally scalable later without a rewrite.

## 2. Workers & queue

No external queue. The `monitors` table *is* the queue; `next_check_at` is the priority field. Dispatch to a bounded `ThreadPoolExecutor` (default 20 workers) inside the scheduler process — each thread runs one check synchronously via `httpx`, writes its `Check` row, and evaluates failure/recovery logic before returning to the pool.

## 3. Concurrency

- Global cap: the thread pool size (config value, tunable per Fly.io VM's CPU/memory).
- Per-monitor: irrelevant — a monitor is only ever "in flight" once, since `next_check_at` is claimed atomically before dispatch.
- No single monitor or check type can starve others: the claim query is FIFO by `next_check_at`, and batch size is capped per poll cycle.

## 4. Timeouts

Configurable per monitor (`config.timeout_seconds`, default 30s, PRD §6.1 advanced settings). The `httpx` client is given `timeout=monitor.config.timeout_seconds`; a timeout is recorded as a failed `Check` with `status_detail="timeout"`.

## 5. Retries (within one check cycle vs. across incidents — two different things)

- **Within one check attempt:** no automatic retry — a slow/flaky single request is exactly what multi-location confirmation (below) exists to filter out, not per-request retries that would just delay detection.
- **Notification delivery retries:** `NotificationDelivery.attempt_count` with exponential backoff (1m, 5m, 15m, capped), claimed by the same `skip_locked` pattern via `run_notifications`.

## 6. Locations (multi-location checks)

For HTTP(S), Keyword, Ping, and Port monitors: the primary scheduler always checks directly; it also calls the 2 regional prober services (tiny FastAPI apps, see `01-tech-stack.md`) over signed HTTP. A failure only becomes a candidate incident if **at least 2 of 3 regions agree** it's down. SSL, Domain, Cron/Heartbeat, and DNS monitors don't benefit from geographic diversity the same way (a cert either is or isn't expiring; a heartbeat either arrived or didn't) — for those, "confirmation" means a same-region recheck after a short delay instead.

## 7. Check results — what gets stored

Every `Check` row: `region`, `started_at`, `duration_ms`, `success`, `status_detail`, and for HTTP(S) checks specifically the timing breakdown (`dns_ms`, `tcp_ms`, `tls_ms`, `ttfb_ms`) captured via `httpx`'s event hooks — this is the raw data Phase 10's root-cause breakdown (PRD §6.8) presents, not a separate collection step.

## 8. Failure detection

```
Check fails in primary region
        ↓
Was this monitor already "down"? → yes: no new incident, just log the check
        ↓ no
Ask the 2 regional probers to check right now (not wait for their own schedule)
        ↓
≥ 2 of 3 regions report failure? → no: log check as a blip, take no action
        ↓ yes
Confirmed → proceed to incident creation (§10)
```

## 9. Recovery detection

The *next scheduled check* (from the primary region) succeeding is what closes an incident — no separate "recovery confirmation" round-trip, since a single success after a confirmed multi-region failure is a strong enough signal and keeps recovery fast (the PRD's whole positioning is "know quickly," not "be paranoid about recovery too").

## 10. Incident creation

On confirmed failure:
1. Create `Incident` (`started_at = now()`, `reason` from the failing check's `status_detail`).
2. Look up `Deploy` rows for the monitor's tagged `service_name` within the correlation window (default 5 min) before `started_at`; if found, set `Incident.deploy_id`.
3. Compute `root_cause_stage` from the failing check's timing breakdown (whichever of DNS/TCP/TLS/TTFB regressed most vs. that monitor's own 30-day p50 baseline; `None` for non-timed monitor types).
4. Write an `IncidentEvent(kind="opened")`.
5. Enqueue notifications (§12) to every `AlertContact` attached to the monitor.
6. Set `Monitor.status = "down"`.

## 11. Incident resolution

On the next successful check for a monitor with an open incident:
1. Set `Incident.ended_at = now()`.
2. Write `IncidentEvent(kind="resolved")`.
3. Enqueue a recovery notification (§12).
4. Set `Monitor.status = "up"`.
5. Incident becomes eligible for the postmortem template (PRD §6.3) — not auto-generated, user-triggered from the incident detail page.

## 12. Notification system

```
Event (incident opened / still open at recurrence interval / resolved /
       deploy correlated / provider incident / status-page subscriber update)
        ↓
Notification rule: which AlertContacts apply (monitor's attached contacts,
or a provider's subscribed contacts, or a status-page's subscriber list)
        ↓
NotificationDelivery rows created (one per contact per event), status=pending
        ↓
run_notifications claims due rows (skip_locked, same pattern as §1)
        ↓
Channel adapter (Email via Resend / Slack / Discord / Telegram / generic
Webhook / Web push) sends; on failure, backoff + retry (§5); on final
failure after max attempts, status=failed and surfaced in the dashboard
        ↓
Recurring: while Incident.ended_at IS NULL, run_notifications also creates
new pending deliveries on each contact's configured repeat_interval_seconds
```

| Channel | Adapter approach |
| --- | --- |
| Email | Resend API |
| Slack / Discord | Incoming webhook URL, POST a formatted payload |
| Telegram | Bot API `sendMessage` |
| Generic Webhook | POST signed JSON payload (HMAC header, so the receiver can verify it's really RootPulse) |
| Web/mobile push | Web Push API (VAPID keys, no third-party service needed — genuinely free) |

## 13. Per-monitor-type implementation table

| Monitor type | Config (`Monitor.config` JSONB) | Execution | Failure condition | Incident-specific notes |
| --- | --- | --- | --- | --- |
| HTTP(S) | `method`, `headers`, `expected_status`, `timeout_seconds`, `follow_redirects` | `httpx` request, capture full timing breakdown | Non-matching status, timeout, connection error | Full root-cause breakdown applies |
| Keyword | above + `keyword`, `exists` (bool) | Same request, scan response body | Keyword condition not met (even on 200 OK) | Root-cause breakdown still applies (it's still an HTTP request) |
| Ping | `packet_count` | ICMP echo (raw socket — needs `CAP_NET_RAW` in the container, noted in deployment) | No response within timeout | No timing breakdown (not HTTP) |
| Port | `port` | TCP connect | Refused / timeout | No timing breakdown |
| SSL certificate | `warn_days` | TLS handshake, read cert `notAfter` | Days remaining < `warn_days`, or chain invalid | No "incident that recovers" in the usual sense — it's a standing warning until the cert is renewed |
| Domain expiration | `warn_days` | WHOIS/RDAP lookup | Days remaining < `warn_days` | Checked far less often (e.g. daily) — no need for 5-minute polling on a value that changes yearly |
| Cron / Heartbeat | `grace_period_seconds` | **Inverted** — no outbound check; a unique ingest URL (`/api/v1/heartbeat/{token}`) is pinged by the user's job. Scheduler just checks "was the last ping within interval + grace period?" | No ping received in time | `started_at` reasoning is "expected but missing," not a failed request |
| DNS | `record_types` (A/AAAA/CNAME/MX/TXT/NS) | Resolve, diff vs. last-known snapshot stored on the monitor | Unexpected change from the last-known-good snapshot | First-ever check establishes the baseline, doesn't alert |

## 14. Analytics — aggregation strategy

Raw `Check` rows are never queried directly for anything beyond the last 24–48 hours of a single monitor's detail page. Everything else reads from pre-aggregated rollups, computed by a scheduled Django management command (`run_rollups`, hourly):

| Table | Grain | Computed from | Used by |
| --- | --- | --- | --- |
| `CheckRollupHourly` | monitor × hour | raw `Check` rows for that hour | 7d/30d charts, response-time trend |
| `CheckRollupDaily` | monitor × day | `CheckRollupHourly` (rolled up again, not raw checks) | 90d/365d charts, status-page uptime bars |
| `FleetSummaryDaily` | user × day | all monitors' `CheckRollupDaily` for that user | fleet-wide overview dashboard (PRD §6.5) |

Raw `Check` rows older than 90 days (PRD NFR) are deleted after their data is folded into `CheckRollupDaily` — satisfying the retention requirement without unbounded table growth. This is exactly the "don't query millions of raw rows for a dashboard chart" concern the plan explicitly needs to avoid, addressed structurally rather than with ad-hoc caching.

"Top offenders," MTTR, and % of incidents deploy-correlated (PRD §6.5 fleet view) are computed from `Incident`/`Deploy` directly (much lower volume than `Check`) at request time, with the in-process cache (§`01-tech-stack.md`) covering repeat dashboard loads within a short TTL.
