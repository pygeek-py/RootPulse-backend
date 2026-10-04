import ipaddress
import json
from pathlib import Path

import pytest

from monitoring import target_validation as tv

CASES = json.loads((Path(__file__).parent / "fixtures" / "ssrf_cases.json").read_text())


@pytest.mark.parametrize("ip", CASES["ips"]["forbidden"])
def test_forbidden_ips(ip):
    assert tv.ip_is_forbidden(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", CASES["ips"]["allowed"])
def test_public_ips_are_allowed(ip):
    assert not tv.ip_is_forbidden(ipaddress.ip_address(ip))


@pytest.mark.parametrize("host", CASES["hosts_forbidden"])
def test_forbidden_hosts(host):
    with pytest.raises(tv.TargetRejected):
        tv.validate_public_host(host)


@pytest.mark.parametrize("url", CASES["urls_forbidden"])
def test_forbidden_urls(url):
    with pytest.raises(tv.TargetRejected):
        tv.validate_http_url(url)


@pytest.mark.parametrize("url", CASES["urls_allowed"])
def test_allowed_urls(url):
    assert tv.validate_http_url(url) == url.strip()


class TestResolution:
    def test_a_public_name_that_resolves_to_a_private_ip_is_refused(self, dns):
        dns["sneaky.example.com"] = ["10.0.0.5"]
        with pytest.raises(tv.TargetRejected, match="private or reserved"):
            tv.validate_http_url("https://sneaky.example.com/")

    def test_one_bad_address_among_good_ones_is_enough_to_refuse(self, dns):
        dns["mixed.example.com"] = ["93.184.216.34", "169.254.169.254"]
        with pytest.raises(tv.TargetRejected):
            tv.validate_public_host("mixed.example.com")

    def test_ipv6_answers_are_judged_too(self, dns):
        dns["v6.example.com"] = ["::1"]
        with pytest.raises(tv.TargetRejected):
            tv.validate_public_host("v6.example.com")

    def test_an_unresolvable_name_is_allowed_at_creation(self, dns):
        # The site may not be live yet; the check will report it down, and every
        # check re-validates anyway.
        dns["not-live-yet.example.com"] = None
        assert tv.validate_public_host("not-live-yet.example.com") == "not-live-yet.example.com"

    def test_check_time_resolution_returns_the_vetted_ips(self):
        assert [str(ip) for ip in tv.resolve_public_ips("example.com")] == ["93.184.216.34"]

    def test_check_time_resolution_catches_a_rebound_name(self, dns):
        assert tv.resolve_public_ips("rebind.example.com")  # safe when validated...
        dns["rebind.example.com"] = ["127.0.0.1"]  # ...then repointed before the check runs
        with pytest.raises(tv.TargetRejected):
            tv.resolve_public_ips("rebind.example.com")

    def test_check_time_resolution_reports_unresolvable_hosts(self, dns):
        dns["gone.example.com"] = None
        with pytest.raises(tv.ResolutionFailed):
            tv.resolve_public_ips("gone.example.com")

    def test_check_time_resolution_refuses_literals(self):
        with pytest.raises(tv.TargetRejected):
            tv.resolve_public_ips("169.254.169.254")


class TestHostSyntax:
    def test_normalises_case_and_trailing_dot(self):
        assert tv.validate_public_host("Example.COM.") == "example.com"

    def test_accepts_idn_names(self):
        assert tv.validate_public_host("bücher.example") == "xn--bcher-kva.example"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "-bad.example.com",
            "a..b.com",
            "exa_mple.com",
            "a" * 64 + ".com",
            "x" * 250 + ".com",
        ],
    )
    def test_rejects_malformed_names(self, bad):
        with pytest.raises(tv.TargetRejected):
            tv.validate_public_host(bad)

    @pytest.mark.parametrize(
        "bad", ["https://example.com", "example.com/path", "example.com:8080", "user@example.com"]
    )
    def test_rejects_urls_and_ports_in_host_fields(self, bad):
        with pytest.raises(tv.TargetRejected):
            tv.validate_public_host(bad)

    def test_domain_names_must_be_real_domains(self):
        assert tv.validate_domain_name("Example.com") == "example.com"
        for bad in ["example", "1.2.3.4", "example.com/x", "a b.com", "::1"]:
            with pytest.raises(tv.TargetRejected):
                tv.validate_domain_name(bad)
