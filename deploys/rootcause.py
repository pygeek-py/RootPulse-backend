"""Which part of the request went wrong? (docs/plan/03-monitoring-engine.md section 10, step 3)

A request has four stages: DNS lookup, TCP connection, TLS handshake and waiting for the server.
When an HTTP check fails we say where, in one of two ways:

* **The failure names the stage.** A DNS error is a DNS problem, a refused connection is a TCP
  problem, a TLS error is a TLS problem. No baseline is needed.
* **Otherwise, whichever stage regressed most** against that monitor's own recent normal (the
  average for the last 30 days, from the rollups). A stage must be both much slower than normal
  (a ratio) and slower by a meaningful amount (so 2 ms to 5 ms isn't an "incident").

If the server answered but with an error (a 500, a missing keyword) and nothing regressed, the
stage is the server's response. If we genuinely can't tell (a timeout leaves no timings), the
stage is left blank rather than guessed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from analytics.models import CheckRollupDaily
from monitoring.checks import result as r
from monitoring.models import Check, Monitor

BASELINE_DAYS = 30
MIN_RATIO = 2.0
MIN_EXCESS_MS = 100
TIMED_TYPES = {Monitor.Type.HTTP, Monitor.Type.KEYWORD}

STAGES = ("dns", "tcp", "tls", "server")
FIELD = {"dns": "dns_ms", "tcp": "tcp_ms", "tls": "tls_ms", "server": "ttfb_ms"}
NAMED_BY_FAILURE = {
    r.DNS_ERROR: "dns",
    r.CONNECTION_REFUSED: "tcp",
    r.CONNECTION_ERROR: "tcp",
    r.TLS_ERROR: "tls",
}
SERVER_ANSWERED_BADLY = {r.STATUS_MISMATCH, r.KEYWORD_MISSING, r.KEYWORD_FOUND}


def baseline(monitor: Monitor, *, now=None) -> dict[str, int | None]:
    """The monitor's usual time per stage, in ms (None where there is no history)."""
    now = now or timezone.now()
    totals = CheckRollupDaily.objects.filter(
        monitor=monitor, day__gte=(now - timedelta(days=BASELINE_DAYS)).date()
    ).aggregate(
        n=Sum("timing_count"),
        dns=Sum("dns_sum_ms"),
        tcp=Sum("tcp_sum_ms"),
        tls=Sum("tls_sum_ms"),
        server=Sum("ttfb_sum_ms"),
    )
    n = totals["n"] or 0
    if not n:
        return dict.fromkeys(STAGES)
    return {stage: round((totals[stage] or 0) / n) for stage in STAGES}


def analyse(check: Check, usual: dict[str, int | None]) -> dict[str, Any]:
    """The root cause of one failed HTTP check. Empty when there is nothing to say."""
    rows = []
    for stage in STAGES:
        ms = getattr(check, FIELD[stage])
        base = usual.get(stage)
        regressed = False
        if ms is not None and base is not None:
            excess = ms - base
            regressed = excess >= MIN_EXCESS_MS and ms / max(base, 1) >= MIN_RATIO
        rows.append({"stage": stage, "ms": ms, "baseline_ms": base, "regressed": regressed})

    named = NAMED_BY_FAILURE.get(check.status_detail)
    if named:
        stage, basis = named, "failure"
    else:
        worst = [row for row in rows if row["regressed"]]
        if worst:
            stage = max(worst, key=lambda row: row["ms"] - row["baseline_ms"])["stage"]
            basis = "timing"
        elif check.status_detail in SERVER_ANSWERED_BADLY and check.ttfb_ms is not None:
            stage, basis = "server", "response"
        else:
            stage, basis = "", "unknown"

    if not stage and not any(row["ms"] is not None for row in rows):
        return {}
    return {"stage": stage, "basis": basis, "stages": rows}


def compute_for_incident(incident, check: Check) -> dict[str, Any]:
    """Work out and store the root cause for a newly opened incident."""
    monitor = incident.monitor
    if monitor.type not in TIMED_TYPES:
        return {}
    result = analyse(check, baseline(monitor))
    if result:
        incident.root_cause = result
        incident.root_cause_stage = result["stage"]
        incident.save(update_fields=["root_cause", "root_cause_stage"])
    return result
