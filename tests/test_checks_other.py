import ipaddress
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from django.core.cache import cache

from monitoring import target_validation as tv
from monitoring.checks import result as r
from monitoring.checks.dns_check import LookupFailed, NoSuchDomain, run_dns
from monitoring.checks.domain_check import BOOTSTRAP_URL, run_domain
from monitoring.checks.heartbeat_check import evaluate_heartbeat
from monitoring.checks.ssl_check import run_ssl
from monitoring.checks.tcp_check import run_tcp

PUBLIC = [ipaddress.ip_address("93.184.216.34")]
LOOPBACK = [ipaddress.ip_address("127.0.0.1")]
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def resolve_to(ips):
    return lambda host: ips


def refuse(host):
    raise tv.TargetRejected("private")


def vanish(host):
    raise tv.ResolutionFailed(host)


# --- TCP (Port and Ping) -----------------------------------------------------


class TestTcp:
    @pytest.fixture
    def listener(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        yield sock.getsockname()[1]
        sock.close()

    def test_an_open_port_is_up_with_a_connect_time(self, listener):
        result = run_tcp("localhost", listener, 5, resolve=resolve_to(LOOPBACK))
        assert result.success is True and result.status_detail == r.OK
        assert result.tcp_ms is not None and result.dns_ms is not None

    def test_a_closed_port_is_connection_refused(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        result = run_tcp("localhost", closed, 5, resolve=resolve_to(LOOPBACK))
        assert (result.success, result.status_detail) == (False, r.CONNECTION_REFUSED)

    def test_a_timeout_is_down(self):
        def hang(ip, port, timeout):
            raise TimeoutError

        result = run_tcp("example.com", 80, 5, resolve=resolve_to(PUBLIC), connect=hang)
        assert (result.success, result.status_detail) == (False, r.TIMEOUT)

    def test_falls_back_to_the_next_address(self):
        calls = []

        def flaky(ip, port, timeout):
            calls.append(str(ip))
            if len(calls) == 1:
                raise OSError("unreachable")

        ips = [ipaddress.ip_address("2606:4700:4700::1111"), PUBLIC[0]]
        result = run_tcp("example.com", 443, 5, resolve=resolve_to(ips), connect=flaky)
        assert result.success is True and len(calls) == 2

    def test_unresolvable_is_down_but_blocked_is_inconclusive(self):
        assert run_tcp("gone.example.com", 80, 5, resolve=vanish).status_detail == r.DNS_ERROR
        blocked = run_tcp("10.0.0.1", 80, 5, resolve=refuse)
        assert blocked.success is None and blocked.status_detail == r.BLOCKED_TARGET

    def test_never_connects_to_a_forbidden_address_by_default(self, dns):
        dns["sneaky.example.com"] = ["169.254.169.254"]
        result = run_tcp("sneaky.example.com", 80, 5)  # the real resolver and validator
        assert result.success is None and result.status_detail == r.BLOCKED_TARGET


# --- SSL ---------------------------------------------------------------------


def cert(days_from_now, now=NOW):
    expires = now + timedelta(days=days_from_now)
    return {
        "notAfter": expires.strftime("%b %d %H:%M:%S %Y GMT"),
        "issuer": ((("organizationName", "Let's Encrypt"),),),
    }


class TestSsl:
    def run(self, handshake, warn_days=14):
        return run_ssl(
            "example.com",
            443,
            warn_days,
            5,
            resolve=resolve_to(PUBLIC),
            handshake=handshake,
            now=lambda: NOW,
        )

    def test_a_certificate_with_plenty_of_time_is_up(self):
        result = self.run(lambda *a: cert(60))
        assert result.success is True
        assert result.detail["days_remaining"] in (59, 60)
        assert result.detail["issuer"] == "Let's Encrypt"

    def test_a_certificate_inside_the_warning_window_is_down(self):
        result = self.run(lambda *a: cert(5))
        assert (result.success, result.status_detail) == (False, r.CERT_EXPIRING)

    def test_the_warning_threshold_is_configurable(self):
        assert self.run(lambda *a: cert(20), warn_days=30).success is False
        assert self.run(lambda *a: cert(20), warn_days=10).success is True

    def test_an_expired_certificate(self):
        def handshake(*a):
            error = ssl.SSLCertVerificationError(1, "certificate has expired")
            error.verify_code = 10
            error.verify_message = "certificate has expired"
            raise error

        result = self.run(handshake)
        assert (result.success, result.status_detail) == (False, r.CERT_EXPIRED)

    def test_an_untrusted_or_mismatched_certificate_is_a_tls_error(self):
        def handshake(*a):
            error = ssl.SSLCertVerificationError(1, "hostname mismatch")
            error.verify_code = 62
            error.verify_message = "Hostname mismatch"
            raise error

        assert self.run(handshake).status_detail == r.TLS_ERROR

    def test_connection_problems(self):
        def refused(*a):
            raise ConnectionRefusedError

        def timed_out(*a):
            raise TimeoutError

        assert self.run(refused).status_detail == r.CONNECTION_REFUSED
        assert self.run(timed_out).status_detail == r.TIMEOUT

    def test_a_certificate_without_an_expiry_is_inconclusive(self):
        result = self.run(lambda *a: {})
        assert result.success is None

    def test_blocked_hosts_are_inconclusive(self):
        result = run_ssl("10.0.0.1", 443, 14, 5, resolve=refuse)
        assert result.success is None and result.status_detail == r.BLOCKED_TARGET

    def test_a_real_handshake_to_a_closed_port_fails_cleanly(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        result = run_ssl("localhost", closed, 14, 5, resolve=resolve_to(LOOPBACK))
        assert (result.success, result.status_detail) == (False, r.CONNECTION_REFUSED)

    def test_a_server_that_does_not_speak_tls_is_a_tls_error(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def talk_plain():
            conn, _ = server.accept()
            conn.recv(4096)  # swallow the ClientHello, then answer in plain text
            conn.sendall(b"HTTP/1.1 400 not tls\r\n\r\n")
            conn.close()

        threading.Thread(target=talk_plain, daemon=True).start()
        result = run_ssl("localhost", port, 14, 5, resolve=resolve_to(LOOPBACK))
        server.close()
        assert (result.success, result.status_detail) == (False, r.TLS_ERROR)


# --- DNS ---------------------------------------------------------------------


class TestDns:
    @staticmethod
    def lookup(table):
        def fn(domain, rtype):
            answer = table[rtype]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return fn

    def test_the_first_check_is_a_baseline_and_counts_as_up(self):
        result, snapshot = run_dns(
            "example.com",
            ["A", "MX"],
            None,
            lookup=self.lookup({"A": ["1.1.1.1"], "MX": ["10 mx."]}),
        )
        assert result.success is True and result.status_detail == r.DNS_BASELINE
        assert snapshot == {"A": ["1.1.1.1"], "MX": ["10 mx."]}

    def test_unchanged_records_are_up(self):
        previous = {"A": ["1.1.1.1"]}
        result, snapshot = run_dns(
            "example.com", ["A"], previous, lookup=self.lookup({"A": ["1.1.1.1"]})
        )
        assert (result.success, result.status_detail) == (True, r.OK)
        assert snapshot == previous

    def test_a_change_fails_once_and_becomes_the_new_baseline(self):
        previous = {"A": ["1.1.1.1"]}
        lookup = self.lookup({"A": ["6.6.6.6"]})
        result, snapshot = run_dns("example.com", ["A"], previous, lookup=lookup)
        assert (result.success, result.status_detail) == (False, r.DNS_CHANGED)
        assert result.detail["changed"] == {"A": {"before": ["1.1.1.1"], "after": ["6.6.6.6"]}}
        assert snapshot == {"A": ["6.6.6.6"]}
        # The next check, comparing against the new baseline, reads as recovered.
        again, _ = run_dns("example.com", ["A"], snapshot, lookup=lookup)
        assert again.success is True

    def test_only_the_watched_record_types_matter(self):
        previous = {"A": ["1.1.1.1"], "TXT": ["old"]}
        lookup = self.lookup({"A": ["1.1.1.1"], "TXT": ["new"]})
        assert run_dns("example.com", ["A"], previous, lookup=lookup)[0].success is True

    def test_a_vanished_domain_counts_as_records_changing_to_nothing(self):
        previous = {"A": ["1.1.1.1"]}
        result, _ = run_dns(
            "example.com", ["A"], previous, lookup=self.lookup({"A": NoSuchDomain("x")})
        )
        assert result.status_detail == r.DNS_CHANGED
        assert result.detail["changed"]["A"]["after"] == []

    def test_a_resolver_failure_is_not_evidence_of_a_change(self):
        previous = {"A": ["1.1.1.1"]}
        result, snapshot = run_dns(
            "example.com", ["A"], previous, lookup=self.lookup({"A": LookupFailed("SERVFAIL")})
        )
        assert result.success is None and result.status_detail == r.LOOKUP_UNAVAILABLE
        assert snapshot is None  # keep the stored baseline


# --- domain expiry via RDAP --------------------------------------------------

BOOTSTRAP = {"services": [[["com", "net"], ["https://rdap.verisign.example/com/v1/"]]]}


def rdap_transport(domain_status=200, expires=None, bootstrap=BOOTSTRAP, calls=None):
    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(str(request.url))
        if str(request.url) == BOOTSTRAP_URL:
            return httpx.Response(200, json=bootstrap)
        if request.url.path.startswith("/com/v1/domain/"):
            if domain_status != 200:
                return httpx.Response(domain_status)
            events = [{"eventAction": "registration", "eventDate": "2010-01-01T00:00:00Z"}]
            if expires:
                events.append({"eventAction": "expiration", "eventDate": expires.isoformat()})
            return httpx.Response(200, json={"events": events})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


class TestDomain:
    def run(self, transport, warn_days=30):
        return run_domain("example.com", warn_days, transport=transport, now=lambda: NOW)

    def test_a_domain_with_time_left_is_up(self):
        result = self.run(rdap_transport(expires=NOW + timedelta(days=200)))
        assert result.success is True and result.detail["days_remaining"] == 200

    def test_a_domain_about_to_expire_is_down(self):
        result = self.run(rdap_transport(expires=NOW + timedelta(days=10)))
        assert (result.success, result.status_detail) == (False, r.DOMAIN_EXPIRING)

    def test_an_expired_domain_is_down(self):
        assert self.run(rdap_transport(expires=NOW - timedelta(days=1))).success is False

    def test_the_bootstrap_is_cached(self):
        calls = []
        transport = rdap_transport(expires=NOW + timedelta(days=200), calls=calls)
        self.run(transport)
        self.run(transport)
        assert sum(1 for c in calls if c == BOOTSTRAP_URL) == 1

    @pytest.mark.parametrize(
        "transport",
        [
            rdap_transport(bootstrap={"services": []}),  # a TLD with no RDAP
            rdap_transport(domain_status=404),
            rdap_transport(domain_status=500),
            rdap_transport(domain_status=429),
            rdap_transport(),  # a reply with no expiration event
        ],
    )
    def test_anything_unanswerable_is_inconclusive_not_down(self, transport):
        result = self.run(transport)
        assert result.success is None and result.status_detail == r.LOOKUP_UNAVAILABLE

    def test_an_unreachable_registry_is_inconclusive(self):
        def boom(request):
            raise httpx.ConnectError("down", request=request)

        assert self.run(httpx.MockTransport(boom)).success is None


# --- heartbeat ---------------------------------------------------------------


class TestHeartbeat:
    CREATED = NOW

    def evaluate(self, last_ping, now, interval=3600, grace=300):
        return evaluate_heartbeat(
            created_at=self.CREATED,
            last_ping=last_ping,
            interval_seconds=interval,
            grace_seconds=grace,
            now=now,
        )

    def test_a_recent_ping_is_up(self):
        ping = NOW + timedelta(minutes=50)
        assert self.evaluate(ping, NOW + timedelta(minutes=65)).success is True

    def test_a_ping_that_is_too_old_is_down(self):
        ping = NOW + timedelta(minutes=10)
        result = self.evaluate(ping, NOW + timedelta(minutes=10 + 66))
        assert (result.success, result.status_detail) == (False, r.HEARTBEAT_MISSED)

    def test_never_pinged_is_down_once_the_first_deadline_passes(self):
        assert self.evaluate(None, NOW + timedelta(minutes=66)).success is False

    def test_the_grace_period_is_honoured(self):
        ping = NOW
        assert self.evaluate(ping, NOW + timedelta(seconds=3600 + 299)).success is True
        assert self.evaluate(ping, NOW + timedelta(seconds=3600 + 301)).success is False
