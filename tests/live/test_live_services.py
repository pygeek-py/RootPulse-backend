"""The checks against real services: real DNS, real TLS handshakes, real HTTP, real RDAP.

The rest of the suite stubs the network, which is right for speed and repeatability but means a
change in how the real world answers (a resolver quirk, a certificate format, a registry's reply)
would never be noticed. These run the same code paths against a handful of stable public
targets, so they are slow, need the internet, and are left out of a normal run:

    pytest -m live tests/live

Targets are chosen to be boring: example.com (reserved by IANA for exactly this), github.com and
wikipedia.org (large, long-lived), and a couple of public wildcard DNS names that resolve to
private addresses, which is the real-world form of the SSRF trick.

A test is skipped, not failed, when the network itself is unreachable, so an offline laptop
doesn't look like a regression.
"""

import socket

import pytest

from monitoring import target_validation as tv
from monitoring.checks import result as r
from monitoring.checks.dns_check import run_dns
from monitoring.checks.domain_check import run_domain
from monitoring.checks.http_check import HttpChecker
from monitoring.checks.ssl_check import run_ssl
from monitoring.checks.tcp_check import run_tcp

pytestmark = pytest.mark.live


def _online() -> bool:
    try:
        socket.create_connection(("example.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def require_internet():
    if not _online():
        pytest.skip("no route to example.com: the live tests need the internet")


class TestHttp:
    def test_a_real_page_is_up_and_every_phase_is_timed(self):
        result = HttpChecker().run("https://example.com", {})
        assert (result.success, result.status_detail, result.status_code) == (True, r.OK, 200)
        for phase in (result.dns_ms, result.tcp_ms, result.tls_ms, result.ttfb_ms):
            assert phase is not None and phase >= 0
        assert result.duration_ms >= result.ttfb_ms

    def test_a_keyword_that_is_on_the_page_is_up(self):
        result = HttpChecker().run("https://example.com", {}, keyword="Example Domain")
        assert result.success is True

    def test_a_keyword_that_is_not_on_the_page_is_down(self):
        result = HttpChecker().run("https://example.com", {}, keyword="zz-not-on-this-page-zz")
        assert (result.success, result.status_detail) == (False, r.KEYWORD_MISSING)

    def test_a_missing_page_is_a_status_mismatch_with_its_code(self):
        result = HttpChecker().run("https://example.com/rootpulse-qa-does-not-exist", {})
        assert result.success is False and result.status_code == 404
        assert result.status_detail == r.STATUS_MISMATCH

    def test_a_real_redirect_is_followed_and_counted(self):
        result = HttpChecker().run("http://github.com/", {"follow_redirects": True})
        assert result.success is True and result.status_code == 200
        assert result.detail.get("redirects", 0) >= 1

    def test_with_redirects_off_the_redirect_itself_is_the_answer(self):
        result = HttpChecker().run("http://github.com/", {"follow_redirects": False})
        # A redirect is a valid reply (the default accepts 2xx and 3xx); it is simply not followed.
        assert result.status_code in (301, 302, 308) and result.detail["redirects"] == 0

    def test_a_name_that_does_not_exist_is_a_dns_error(self):
        result = HttpChecker().run("https://rootpulse-qa-nonexistent-12345.example.com", {})
        assert (result.success, result.status_detail) == (False, r.DNS_ERROR)


class TestTcp:
    def test_a_real_port_is_up(self):
        result = run_tcp("example.com", 443, 10)
        assert result.success is True and result.tcp_ms is not None

    def test_a_name_that_does_not_exist_is_a_dns_error(self):
        result = run_tcp("rootpulse-qa-nonexistent-12345.example.com", 443, 10)
        assert (result.success, result.status_detail) == (False, r.DNS_ERROR)


class TestSsl:
    def test_a_real_certificate_is_read_and_has_time_left(self):
        result = run_ssl("example.com", 443, 14, 10)
        assert result.success is True, result.detail
        assert result.detail.get("days_remaining", 0) > 0 or result.status_detail == r.OK

    def test_a_threshold_longer_than_the_certificate_warns(self):
        result = run_ssl("example.com", 443, 100_000, 10)
        assert (result.success, result.status_detail) == (False, r.CERT_EXPIRING)


class TestDns:
    def test_real_records_are_read_then_compared(self):
        first, snapshot = run_dns("example.com", ["A", "NS"], None)
        assert first.success is True and first.status_detail == r.DNS_BASELINE
        assert snapshot["A"] and snapshot["NS"]

        unchanged, _ = run_dns("example.com", ["A", "NS"], snapshot)
        assert unchanged.success is True

        tampered = {**snapshot, "A": ["192.0.2.1"]}
        changed, _ = run_dns("example.com", ["A", "NS"], tampered)
        assert (changed.success, changed.status_detail) == (False, r.DNS_CHANGED)

    def test_a_domain_that_does_not_exist_has_no_records(self):
        result, snapshot = run_dns("rootpulse-qa-nonexistent-12345.com", ["A"], None)
        assert result.success is not False or snapshot == {"A": []}


class TestDomainExpiry:
    def test_a_real_registration_is_looked_up(self):
        result = run_domain("wikipedia.org", 30)
        if result.success is None:
            pytest.skip(f"RDAP unavailable right now: {result.detail}")
        assert result.success is True, result.detail


class TestSsrfWithRealResolvers:
    """These names are public and resolve (over real DNS) to addresses we must never connect to:
    the real-world form of the trick the validator exists for."""

    @pytest.mark.parametrize(
        "host",
        ["localtest.me", "127.0.0.1.nip.io", "169.254.169.254.nip.io", "10.0.0.1.nip.io"],
    )
    def test_a_public_name_that_points_inside_is_refused_at_creation_and_at_check_time(self, host):
        try:
            tv.resolve_host(host)
        except tv.ResolutionFailed:
            pytest.skip(f"{host} didn't resolve from here")
        with pytest.raises(tv.TargetRejected):
            tv.validate_public_host(host)
        with pytest.raises(tv.TargetRejected):
            tv.validate_http_url(f"http://{host}/")
        with pytest.raises(tv.TargetRejected):
            tv.resolve_public_ips(host)

    def test_and_a_check_of_it_never_connects(self):
        result = HttpChecker().run("http://127.0.0.1.nip.io/", {})
        assert result.success is None and result.status_detail == r.BLOCKED_TARGET

    @pytest.mark.parametrize("host", ["localhost", "127.1", "0x7f.0.0.1", "2130706433"])
    def test_loopback_in_every_spelling_is_refused_with_the_real_resolver(self, host):
        with pytest.raises(tv.TargetRejected):
            tv.validate_public_host(host)


@pytest.mark.django_db
class TestProviderFeeds:
    """Every curated provider's real status feed, through the real adapters. The canary for a
    provider changing its feed's shape or moving (as Anthropic's status page did)."""

    def test_every_catalogue_provider_is_readable_and_parses(self):
        from providers import poller
        from providers.models import Provider

        failures = []
        with poller.new_client() as client:
            for provider in Provider.objects.filter(enabled=True):
                try:
                    result = poller.read_feed(provider, client=client)
                except poller.FeedError as exc:
                    failures.append(f"{provider.slug}: {exc}")
                    continue
                if not result.status or result.incidents is None:
                    failures.append(f"{provider.slug}: empty result")
        assert failures == []
