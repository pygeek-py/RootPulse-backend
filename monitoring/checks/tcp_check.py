"""TCP connect checks: the Port monitor, and "Ping" (which is a TCP connect too,
because neither Render nor Cloudflare Workers can send ICMP: docs/plan/03-monitoring-engine.md)."""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable

from .. import target_validation as tv
from . import result as r

Connect = Callable[[tv.IPAddress, int, float], None]


def _connect(ip: tv.IPAddress, port: int, timeout: float) -> None:
    family = socket.AF_INET6 if isinstance(ip, ipaddress.IPv6Address) else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((str(ip), port))


def run_tcp(
    host: str,
    port: int,
    timeout: float,
    *,
    resolve: Callable[[str], list[tv.IPAddress]] = tv.resolve_public_ips,
    connect: Connect = _connect,
    clock: Callable[[], float] = time.perf_counter,
) -> r.CheckResult:
    started = clock()
    try:
        ips = resolve(host)
    except tv.TargetRejected as exc:
        return r.CheckResult.inconclusive(r.BLOCKED_TARGET, reason=str(exc))
    except tv.ResolutionFailed:
        return r.CheckResult(
            success=False, status_detail=r.DNS_ERROR, duration_ms=r.elapsed_ms(started, clock())
        )
    dns_ms = r.elapsed_ms(started, clock())

    outcome = r.CONNECTION_ERROR
    for ip in ips:
        remaining = timeout - (clock() - started)
        if remaining <= 0:
            outcome = r.TIMEOUT
            break
        attempt = clock()
        try:
            connect(ip, port, remaining)
        except ConnectionRefusedError:
            outcome = r.CONNECTION_REFUSED
        except (TimeoutError, socket.timeout):
            outcome = r.TIMEOUT
        except OSError:
            outcome = r.CONNECTION_ERROR
        else:
            tcp_ms = r.elapsed_ms(attempt, clock())
            return r.CheckResult(
                success=True,
                status_detail=r.OK,
                duration_ms=r.elapsed_ms(started, clock()),
                dns_ms=dns_ms,
                tcp_ms=tcp_ms,
                detail={"address": str(ip), "port": port},
            )
    return r.CheckResult(
        success=False,
        status_detail=outcome,
        duration_ms=r.elapsed_ms(started, clock()),
        dns_ms=dns_ms,
        detail={"port": port},
    )
