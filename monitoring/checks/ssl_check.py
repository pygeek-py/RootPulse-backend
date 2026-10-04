"""SSL certificate monitor: a real TLS handshake, then days until `notAfter`."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .. import target_validation as tv
from . import result as r

# OpenSSL's verify code for "certificate has expired".
X509_V_ERR_CERT_HAS_EXPIRED = 10


def _handshake(ip: tv.IPAddress, port: int, host: str, timeout: float) -> dict[str, Any]:
    """Connect to the vetted IP, verify the chain against `host`, return the peer cert."""
    context = ssl.create_default_context()
    family = socket.AF_INET6 if isinstance(ip, ipaddress.IPv6Address) else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as raw:
        raw.settimeout(timeout)
        raw.connect((str(ip), port))
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert() or {}


def _issuer(cert: dict[str, Any]) -> str:
    for group in cert.get("issuer", ()):
        for key, value in group:
            if key in ("organizationName", "commonName"):
                return str(value)
    return ""


def run_ssl(
    host: str,
    port: int,
    warn_days: int,
    timeout: float,
    *,
    resolve: Callable[[str], list[tv.IPAddress]] = tv.resolve_public_ips,
    handshake: Callable[..., dict[str, Any]] = _handshake,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
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

    outcome = r.CONNECTION_ERROR
    for ip in ips:
        try:
            cert = handshake(ip, port, host, max(1.0, timeout - (clock() - started)))
        except ssl.SSLCertVerificationError as exc:
            expired = exc.verify_code == X509_V_ERR_CERT_HAS_EXPIRED
            return r.CheckResult(
                success=False,
                status_detail=r.CERT_EXPIRED if expired else r.TLS_ERROR,
                duration_ms=r.elapsed_ms(started, clock()),
                detail={"reason": exc.verify_message},
            )
        except ssl.SSLError as exc:
            return r.CheckResult(
                success=False,
                status_detail=r.TLS_ERROR,
                duration_ms=r.elapsed_ms(started, clock()),
                detail={"reason": exc.reason or "handshake_failed"},
            )
        except ConnectionRefusedError:
            outcome = r.CONNECTION_REFUSED
        except (TimeoutError, socket.timeout):
            outcome = r.TIMEOUT
        except OSError:
            outcome = r.CONNECTION_ERROR
        else:
            not_after = cert.get("notAfter")
            if not not_after:
                return r.CheckResult.inconclusive(
                    r.LOOKUP_UNAVAILABLE, reason="no_expiry_in_certificate"
                )
            expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=timezone.utc)
            days = (expires - now()).days
            ok = days >= warn_days
            return r.CheckResult(
                success=ok,
                status_detail=r.OK if ok else r.CERT_EXPIRING,
                duration_ms=r.elapsed_ms(started, clock()),
                detail={
                    "days_remaining": days,
                    "not_after": expires.isoformat(),
                    "issuer": _issuer(cert),
                },
            )
    return r.CheckResult(
        success=False, status_detail=outcome, duration_ms=r.elapsed_ms(started, clock())
    )
