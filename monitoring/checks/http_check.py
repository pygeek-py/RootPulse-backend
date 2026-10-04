"""HTTP(S) and keyword checks.

Safety properties (docs/plan/04-security.md section 9):

* Every hop, including each redirect, is resolved and judged by address before
  any connection is made.
* The connection goes to the *vetted IP*, not the name, so a DNS answer that
  changes between validation and connecting (rebinding) can't redirect us.
  TLS still validates the certificate against the real host name (SNI is set
  explicitly) and the Host header carries the real name.
* Response bodies are capped, and the whole check has a hard deadline.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings

from .. import target_validation as tv
from . import result as r

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 1_000_000
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_STATUS_SPEC = re.compile(r"^([1-5][0-9]{2})$|^([1-5])xx$")

Resolver = Callable[[str], list[tv.IPAddress]]


def status_matches(code: int, specs: list[str]) -> bool:
    """Is `code` allowed by specs like ["200", "3xx"]?"""
    for spec in specs:
        match = _STATUS_SPEC.match(spec.lower())
        if not match:
            continue
        if match.group(1) and int(match.group(1)) == code:
            return True
        if match.group(2) and code // 100 == int(match.group(2)):
            return True
    return False


def _root_cause(exc: BaseException) -> BaseException:
    while exc.__cause__ is not None or exc.__context__ is not None:
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return exc


def classify_error(exc: BaseException) -> str:
    """Map a transport exception onto one of our outcome names."""
    if isinstance(exc, httpx.TimeoutException):
        return r.TIMEOUT
    chain: list[BaseException] = []
    cursor: BaseException | None = exc
    while cursor is not None and cursor not in chain:
        chain.append(cursor)
        cursor = cursor.__cause__ or cursor.__context__
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return r.TLS_ERROR
    if any(isinstance(e, ConnectionRefusedError) for e in chain):
        return r.CONNECTION_REFUSED
    if any(isinstance(e, (socket.timeout, TimeoutError)) for e in chain):
        return r.TIMEOUT
    return r.CONNECTION_ERROR


class _Trace:
    """Collects connection timings from httpcore's trace events."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._t: dict[str, float] = {}

    def __call__(self, event: str, info: dict[str, Any]) -> None:
        self._t[event] = self._clock()

    def _span(self, start: str, end: str) -> int | None:
        if start in self._t and end in self._t:
            return r.elapsed_ms(self._t[start], self._t[end])
        return None

    @property
    def tcp_ms(self) -> int | None:
        return self._span("connection.connect_tcp.started", "connection.connect_tcp.complete")

    @property
    def tls_ms(self) -> int | None:
        return self._span("connection.start_tls.started", "connection.start_tls.complete")

    @property
    def ttfb_ms(self) -> int | None:
        return self._span(
            "http11.send_request_headers.started", "http11.receive_response_headers.complete"
        )


def _host_header(parts, host: str) -> str:
    default = 443 if parts.scheme == "https" else 80
    shown = f"[{host}]" if ":" in host else host
    return shown if parts.port in (None, default) else f"{shown}:{parts.port}"


def _ip_url(parts, ip: tv.IPAddress) -> str:
    shown = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
    port = f":{parts.port}" if parts.port else ""
    target = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{shown}{port}{target}{query}"


class HttpChecker:
    def __init__(
        self,
        *,
        resolve: Resolver = tv.resolve_public_ips,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self._resolve = resolve
        self._transport = transport
        self._clock = clock

    def run(self, target: str, config: dict[str, Any], keyword: str | None = None) -> r.CheckResult:
        clock = self._clock
        started = clock()
        timeout = float(config.get("timeout_seconds", 10))
        deadline = started + timeout
        method = str(config.get("method", "GET")).upper()
        body = str(config.get("body", "")) if method in BODY_METHODS else ""
        specs = list(config.get("expected_status") or ["2xx", "3xx"])
        follow = bool(config.get("follow_redirects", True))
        user_headers = dict(config.get("headers") or {})

        url = target
        dns_total = 0
        redirects = 0
        trace = _Trace(clock)
        client = httpx.Client(
            transport=self._transport or httpx.HTTPTransport(retries=0),
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )
        try:
            while True:
                parts = urlsplit(url)
                host = parts.hostname or ""
                if parts.scheme not in ("http", "https") or not host or parts.username:
                    return r.CheckResult.inconclusive(r.BLOCKED_TARGET, reason="invalid_url")

                # Resolve and vet this hop's host (the original URL and every redirect target).
                dns_start = clock()
                try:
                    ips = self._resolve(host)
                except tv.TargetRejected as exc:
                    return r.CheckResult.inconclusive(r.BLOCKED_TARGET, reason=str(exc))
                except tv.ResolutionFailed:
                    return r.CheckResult(
                        success=False,
                        status_detail=r.DNS_ERROR,
                        duration_ms=r.elapsed_ms(started, clock()),
                    )
                dns_total += r.elapsed_ms(dns_start, clock())

                headers = {
                    "User-Agent": settings.CHECK_USER_AGENT,
                    "Accept": "*/*",
                    **user_headers,
                    "Host": _host_header(parts, host),
                }
                extensions: dict[str, Any] = {"trace": trace}
                if parts.scheme == "https":
                    extensions["sni_hostname"] = host

                response_info = self._send_to_any(
                    client, method, parts, ips, headers, body, extensions, deadline
                )
                if isinstance(response_info, r.CheckResult):
                    response_info.duration_ms = r.elapsed_ms(started, clock())
                    response_info.dns_ms = dns_total
                    return response_info
                status_code, location, page, address = response_info

                if follow and status_code in REDIRECT_STATUSES and location:
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        return self._finish(
                            r.TOO_MANY_REDIRECTS,
                            False,
                            status_code,
                            started,
                            dns_total,
                            trace,
                            {"redirects": redirects},
                        )
                    url = urljoin(url, location)
                    if status_code in (301, 302, 303) and method not in ("GET", "HEAD"):
                        method, body = "GET", ""
                    continue

                detail = {"redirects": redirects, "address": str(address)}
                if not status_matches(status_code, specs):
                    return self._finish(
                        r.STATUS_MISMATCH, False, status_code, started, dns_total, trace, detail
                    )
                if keyword is not None:
                    present = keyword in page
                    wants_present = bool(config.get("exists", True))
                    if present != wants_present:
                        outcome = r.KEYWORD_MISSING if wants_present else r.KEYWORD_FOUND
                        return self._finish(
                            outcome, False, status_code, started, dns_total, trace, detail
                        )
                return self._finish(r.OK, True, status_code, started, dns_total, trace, detail)
        finally:
            client.close()

    # -- helpers ---------------------------------------------------------

    def _finish(
        self, outcome, success, status_code, started, dns_ms, trace, detail
    ) -> r.CheckResult:
        return r.CheckResult(
            success=success,
            status_detail=outcome,
            status_code=status_code,
            duration_ms=r.elapsed_ms(started, self._clock()),
            dns_ms=dns_ms,
            tcp_ms=trace.tcp_ms,
            tls_ms=trace.tls_ms,
            ttfb_ms=trace.ttfb_ms,
            detail=detail,
        )

    def _send_to_any(self, client, method, parts, ips, headers, body, extensions, deadline):
        """Try each vetted address in turn (a dual-stack host may be unreachable over IPv6).

        Returns (status, location, body_text, address), or a CheckResult for a failure.
        """
        last_failure = r.CONNECTION_ERROR
        for ip in ips:
            if self._clock() >= deadline:
                return r.CheckResult(success=False, status_detail=r.TIMEOUT)
            request = client.build_request(
                method,
                _ip_url(parts, ip),
                headers=headers,
                content=body.encode() if body else None,
                extensions=extensions,
            )
            try:
                response = client.send(request, stream=True)
            except httpx.HTTPError as exc:
                last_failure = classify_error(exc)
                if last_failure in (r.TIMEOUT, r.TLS_ERROR):
                    break  # another address won't change a timeout or a bad certificate
                continue
            try:
                page = self._read_body(response, deadline)
            except _Deadline:
                response.close()
                return r.CheckResult(success=False, status_detail=r.TIMEOUT)
            except httpx.HTTPError as exc:
                response.close()
                return r.CheckResult(success=False, status_detail=classify_error(exc))
            finally:
                response.close()
            return response.status_code, response.headers.get("location"), page, ip
        return r.CheckResult(success=False, status_detail=last_failure)

    def _read_body(self, response: httpx.Response, deadline: float) -> str:
        """Read up to MAX_BODY_BYTES (decoded), within the deadline, and return it as text."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            if self._clock() >= deadline:
                raise _Deadline
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BODY_BYTES:
                break
        data = b"".join(chunks)[:MAX_BODY_BYTES]  # a single chunk can overshoot the cap
        encoding = response.charset_encoding or "utf-8"
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")


class _Deadline(Exception):
    pass
