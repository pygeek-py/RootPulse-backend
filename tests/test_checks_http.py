import ipaddress
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from monitoring import target_validation as tv
from monitoring.checks import result as r
from monitoring.checks.http_check import HttpChecker, classify_error, status_matches

PUBLIC = [ipaddress.ip_address("93.184.216.34")]


def resolver(mapping=None):
    mapping = mapping or {}

    def resolve(host):
        answer = mapping.get(host, PUBLIC)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return resolve


def checker(handler, mapping=None):
    seen = []

    def wrapped(request: httpx.Request):
        seen.append(request)
        return handler(request)

    return HttpChecker(resolve=resolver(mapping), transport=httpx.MockTransport(wrapped)), seen


def ok(body="hello", status=200, **kwargs):
    return lambda request: httpx.Response(status, text=body, **kwargs)


class TestStatusMatching:
    @pytest.mark.parametrize(
        "code,specs,expected",
        [
            (200, ["2xx"], True),
            (204, ["200"], False),
            (301, ["2xx", "3xx"], True),
            (404, ["2xx", "3xx"], False),
            (404, ["404"], True),
            (503, ["5xx"], True),
            (200, ["bogus"], False),
            (200, [], False),
        ],
    )
    def test_specs(self, code, specs, expected):
        assert status_matches(code, specs) is expected


class TestOutcomes:
    def test_a_healthy_page_is_up(self):
        c, _ = checker(ok())
        result = c.run("https://example.com/", {})
        assert result.success is True and result.status_detail == r.OK
        assert result.status_code == 200 and result.duration_ms is not None

    def test_an_unexpected_status_is_down_and_records_the_code(self):
        c, _ = checker(ok(status=503))
        result = c.run("https://example.com/", {})
        assert (result.success, result.status_detail, result.status_code) == (
            False,
            r.STATUS_MISMATCH,
            503,
        )

    def test_expected_status_is_configurable(self):
        c, _ = checker(ok(status=404))
        assert c.run("https://example.com/", {"expected_status": ["404"]}).success is True

    def test_a_timeout_is_down(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        c, _ = checker(handler)
        result = c.run("https://example.com/", {})
        assert (result.success, result.status_detail) == (False, r.TIMEOUT)

    def test_connection_refused_is_distinguished(self):
        def handler(request):
            error = httpx.ConnectError("refused", request=request)
            error.__cause__ = ConnectionRefusedError()
            raise error

        c, _ = checker(handler)
        assert c.run("https://example.com/", {}).status_detail == r.CONNECTION_REFUSED

    def test_a_bad_certificate_is_a_tls_error(self):
        import ssl

        def handler(request):
            error = httpx.ConnectError("tls", request=request)
            error.__cause__ = ssl.SSLCertVerificationError("expired")
            raise error

        c, _ = checker(handler)
        result = c.run("https://example.com/", {})
        assert (result.success, result.status_detail) == (False, r.TLS_ERROR)

    def test_an_unresolvable_host_is_a_dns_error_and_down(self):
        c, seen = checker(ok(), {"gone.example.com": tv.ResolutionFailed("gone.example.com")})
        result = c.run("https://gone.example.com/", {})
        assert (result.success, result.status_detail) == (False, r.DNS_ERROR)
        assert seen == []

    def test_a_blocked_target_is_inconclusive_never_down(self):
        # Refusing to connect is our decision, not evidence the site is broken.
        c, seen = checker(ok(), {"evil.example.com": tv.TargetRejected("private")})
        result = c.run("https://evil.example.com/", {})
        assert result.success is None and result.status_detail == r.BLOCKED_TARGET
        assert seen == []

    def test_exceptions_are_classified(self):
        assert classify_error(httpx.ConnectTimeout("t")) == r.TIMEOUT
        assert classify_error(httpx.RemoteProtocolError("p")) == r.CONNECTION_ERROR


class TestPinning:
    def test_it_connects_to_the_vetted_ip_with_the_real_host_header_and_sni(self):
        c, seen = checker(ok())
        c.run("https://example.com:8443/a/b?x=1", {})
        request = seen[0]
        assert request.url.host == "93.184.216.34"
        assert request.url.port == 8443
        assert request.url.path == "/a/b" and request.url.query == b"x=1"
        assert request.headers["host"] == "example.com:8443"
        assert request.extensions["sni_hostname"] == "example.com"

    def test_ipv6_addresses_are_bracketed(self):
        c, seen = checker(ok(), {"v6.example.com": [ipaddress.ip_address("2606:4700:4700::1111")]})
        c.run("http://v6.example.com/", {})
        assert seen[0].url.host == "2606:4700:4700::1111"
        assert "sni_hostname" not in seen[0].extensions

    def test_the_first_reachable_address_wins(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "2606:4700:4700::1111":
                raise httpx.ConnectError("no ipv6 route", request=request)
            return httpx.Response(200)

        ips = [ipaddress.ip_address("2606:4700:4700::1111"), ipaddress.ip_address("93.184.216.34")]
        c, _ = checker(handler, {"dual.example.com": ips})
        assert c.run("https://dual.example.com/", {}).success is True
        assert calls == ["2606:4700:4700::1111", "93.184.216.34"]

    def test_a_dns_answer_that_changes_cannot_redirect_the_connection(self):
        # The resolver is consulted once per hop and its answer is what we connect to.
        c, seen = checker(ok())
        c.run("https://example.com/", {})
        assert [str(req.url.host) for req in seen] == ["93.184.216.34"]


class TestRequest:
    def test_sends_method_headers_body_and_a_user_agent(self):
        c, seen = checker(ok())
        c.run(
            "https://example.com/",
            {
                "method": "POST",
                "body": '{"a":1}',
                "headers": {"X-Token": "abc", "User-Agent": "Custom/1"},
            },
        )
        request = seen[0]
        assert request.method == "POST" and request.content == b'{"a":1}'
        assert request.headers["x-token"] == "abc"
        assert request.headers["user-agent"] == "Custom/1"

    def test_default_user_agent_identifies_rootpulse(self):
        c, seen = checker(ok())
        c.run("https://example.com/", {})
        assert seen[0].headers["user-agent"].startswith("RootPulse/")

    def test_a_get_never_sends_a_body(self):
        c, seen = checker(ok())
        c.run("https://example.com/", {"method": "GET", "body": "x"})
        assert seen[0].content == b""


class TestRedirects:
    @staticmethod
    def redirecting(chain):
        """chain: {url: (status, location)}; anything else is a 200."""

        def handler(request):
            url = f"{request.url.scheme}://{request.headers['host']}{request.url.path}"
            if url in chain:
                status, location = chain[url]
                return httpx.Response(status, headers={"location": location})
            return httpx.Response(200, text="landed")

        return handler

    def test_follows_redirects_and_reports_the_count(self):
        chain = {"https://example.com/old": (301, "/new")}
        c, _ = checker(self.redirecting(chain))
        result = c.run("https://example.com/old", {})
        assert result.success is True and result.detail["redirects"] == 1

    def test_every_redirect_target_is_vetted_before_it_is_followed(self):
        chain = {"https://example.com/": (302, "http://internal.example.net/admin")}
        mapping = {"internal.example.net": tv.TargetRejected("private")}
        c, seen = checker(self.redirecting(chain), mapping)
        result = c.run("https://example.com/", {})
        assert result.success is None and result.status_detail == r.BLOCKED_TARGET
        assert len(seen) == 1  # the internal host was never contacted

    def test_a_redirect_to_a_non_http_scheme_is_refused(self):
        chain = {"https://example.com/": (302, "file:///etc/passwd")}
        c, seen = checker(self.redirecting(chain))
        assert c.run("https://example.com/", {}).status_detail == r.BLOCKED_TARGET
        assert len(seen) == 1

    def test_redirect_loops_stop(self):
        chain = {"https://example.com/loop": (302, "/loop")}
        c, _ = checker(self.redirecting(chain))
        result = c.run("https://example.com/loop", {})
        assert (result.success, result.status_detail) == (False, r.TOO_MANY_REDIRECTS)

    def test_follow_redirects_off_judges_the_redirect_itself(self):
        chain = {"https://example.com/": (301, "/elsewhere")}
        c, seen = checker(self.redirecting(chain))
        # 3xx is in the default "up" set, so a redirect is simply up.
        assert c.run("https://example.com/", {"follow_redirects": False}).success is True
        assert len(seen) == 1
        c, _ = checker(self.redirecting(chain))
        result = c.run(
            "https://example.com/", {"follow_redirects": False, "expected_status": ["2xx"]}
        )
        assert (result.success, result.status_code) == (False, 301)

    def test_a_303_turns_a_post_into_a_get(self):
        methods = []

        def handler(request):
            methods.append(request.method)
            if request.method == "POST":
                return httpx.Response(303, headers={"location": "/done"})
            return httpx.Response(200)

        c, _ = checker(handler)
        c.run("https://example.com/", {"method": "POST", "body": "x"})
        assert methods == ["POST", "GET"]


class TestKeyword:
    def test_present_keyword_is_up(self):
        c, _ = checker(ok("Welcome back, friend"))
        assert c.run("https://example.com/", {}, keyword="Welcome").success is True

    def test_missing_keyword_is_down_even_on_a_200(self):
        c, _ = checker(ok("Server error page"))
        result = c.run("https://example.com/", {}, keyword="Welcome")
        assert (result.success, result.status_detail, result.status_code) == (
            False,
            r.KEYWORD_MISSING,
            200,
        )

    def test_exists_false_alerts_when_the_keyword_appears(self):
        c, _ = checker(ok("Fatal error: database"))
        result = c.run("https://example.com/", {"exists": False}, keyword="Fatal error")
        assert (result.success, result.status_detail) == (False, r.KEYWORD_FOUND)
        c, _ = checker(ok("all good"))
        assert (
            c.run("https://example.com/", {"exists": False}, keyword="Fatal error").success is True
        )

    def test_matching_is_case_sensitive(self):
        c, _ = checker(ok("welcome"))
        assert c.run("https://example.com/", {}, keyword="Welcome").success is False

    def test_the_status_is_judged_before_the_keyword(self):
        c, _ = checker(ok("Welcome", status=500))
        assert (
            c.run("https://example.com/", {}, keyword="Welcome").status_detail == r.STATUS_MISMATCH
        )

    def test_decodes_by_the_declared_charset(self):
        c, _ = checker(
            lambda req: httpx.Response(
                200,
                content="café".encode("latin-1"),
                headers={"content-type": "text/html; charset=latin-1"},
            )
        )
        assert c.run("https://example.com/", {}, keyword="café").success is True

    def test_a_huge_body_is_capped_not_loaded(self):
        big = "x" * 3_000_000 + "NEEDLE"
        c, _ = checker(ok(big))
        # The needle sits past the 1 MB cap: it must not be found (and memory stays bounded).
        assert c.run("https://example.com/", {}, keyword="NEEDLE").success is False


# --- against a real socket: timing breakdown and real timeouts --------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(2)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


class TestRealSocket:
    # Loopback is exactly what the validator forbids, so the tests inject a resolver that
    # allows it. Production code always uses target_validation.resolve_public_ips.
    LOOPBACK = staticmethod(lambda host: [ipaddress.ip_address("127.0.0.1")])

    def test_records_a_timing_breakdown(self, local_server):
        result = HttpChecker(resolve=self.LOOPBACK).run(f"http://localhost:{local_server}/", {})
        assert result.success is True
        assert result.dns_ms is not None and result.tcp_ms is not None
        assert result.ttfb_ms is not None and result.duration_ms is not None
        assert result.tls_ms is None  # plain HTTP
        assert result.duration_ms >= result.ttfb_ms >= 0

    def test_a_slow_server_hits_the_timeout(self, local_server):
        started = time.monotonic()
        result = HttpChecker(resolve=self.LOOPBACK).run(
            f"http://localhost:{local_server}/slow", {"timeout_seconds": 1}
        )
        assert (result.success, result.status_detail) == (False, r.TIMEOUT)
        assert time.monotonic() - started < 1.9  # gave up at the deadline, not after the sleep

    def test_a_closed_port_is_connection_refused(self):
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        result = HttpChecker(resolve=self.LOOPBACK).run(f"http://localhost:{closed}/", {})
        assert (result.success, result.status_detail) == (False, r.CONNECTION_REFUSED)
