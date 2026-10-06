"""Smoke test for a deployed RootPulse API (and, optionally, its website).

    python scripts/smoke_test.py --api https://api.example.com --site https://app.example.com
    SCHEDULER_SHARED_SECRET=... python scripts/smoke_test.py --api ... --site ...  # runs a pass

It checks the things that go wrong on a first deploy and that the test suite can't see, because
they are about the deployment rather than the code: is the API up and answering over HTTPS, are
the security headers there, does the website's origin pass CORS (and a stranger's not), is the
admin where it shouldn't be, is the scheduler trigger closed to the unsigned and open to the
signed. It creates nothing, deletes nothing and needs no account: it only reads, plus one signed
call to the scheduler trigger if you give it the secret (that does real work: one normal pass).

Against a container on your own machine in production mode, add --forwarded-https so the request
looks like it came through the platform's HTTPS proxy.

Exit status 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

STRANGER = "https://evil.example.org"


class Smoke:
    def __init__(
        self,
        api: str,
        site: str | None,
        *,
        cors_origin: str | None = None,
        secret: str | None = None,
        forwarded_https: bool = False,
        client: httpx.Client | None = None,
        cold_start_seconds: int = 90,
    ):
        self.api = api.rstrip("/")
        self.site = site.rstrip("/") if site else None
        # The origin the browser would send; normally the website's own.
        self.cors_origin = (cors_origin or site or "").rstrip("/") or None
        self.secret = secret
        self.forwarded_https = forwarded_https
        self.cold_start_seconds = cold_start_seconds
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.results: list[tuple[bool, str, str]] = []

    # --- helpers ----------------------------------------------------------------------------
    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"User-Agent": "rootpulse-smoke-test"}
        if self.forwarded_https:
            headers["X-Forwarded-Proto"] = "https"
        return {**headers, **(extra or {})}

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = self._headers(kwargs.pop("headers", None))
        return self.client.request(method, self.api + path, headers=headers, **kwargs)

    def record(self, name: str, fn: Callable[[], str | None]) -> None:
        try:
            note = fn()
            self.results.append((True, name, note or ""))
        except AssertionError as exc:
            self.results.append((False, name, str(exc)))
        except httpx.HTTPError as exc:
            self.results.append((False, name, f"request failed: {type(exc).__name__}"))

    # --- checks -----------------------------------------------------------------------------
    def check_up(self) -> str:
        """A free Render service sleeps; allow it a minute or so to wake."""
        deadline = time.monotonic() + self.cold_start_seconds
        last = "no answer"
        while True:
            try:
                response = self.request("GET", "/health/")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    return "answered 200"
                last = f"answered {response.status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                last = type(exc).__name__
            if time.monotonic() > deadline:
                raise AssertionError(f"/health/ never came up ({last})")
            time.sleep(5)

    def check_https_enforced(self) -> str:
        if urlsplit(self.api).scheme != "https" or self.forwarded_https:
            return "skipped (not a public https address)"
        plain = "http://" + self.api.split("://", 1)[1] + "/api/v1/monitors/"
        try:
            response = self.client.get(plain, headers=self._headers())
        except httpx.HTTPError:
            return "plain http is not even accepted"
        assert response.status_code in (
            301,
            302,
            307,
            308,
        ), f"plain http answered {response.status_code}, expected a redirect to https"
        assert response.headers.get("location", "").startswith(
            "https://"
        ), "plain http redirects somewhere that isn't https"
        return "plain http redirects to https"

    def check_anonymous_refused_with_headers(self) -> str:
        response = self.request("GET", "/api/v1/monitors/")
        assert (
            response.status_code == 401
        ), f"an anonymous request to the monitors list answered {response.status_code}, not 401"
        headers = response.headers
        assert headers.get("x-content-type-options") == "nosniff", "missing X-Content-Type-Options"
        assert "no-store" in headers.get("cache-control", ""), "missing Cache-Control: no-store"
        assert "frame-ancestors 'none'" in headers.get(
            "content-security-policy", ""
        ), "missing the API's Content-Security-Policy"
        if urlsplit(self.api).scheme == "https" or self.forwarded_https:
            assert "max-age=" in headers.get(
                "strict-transport-security", ""
            ), "missing Strict-Transport-Security"
        return "401, with the security headers"

    def check_cors(self) -> str:
        if not self.cors_origin:
            return "skipped (no --site)"
        origin = self.cors_origin
        ok = self.request(
            "OPTIONS",
            "/api/v1/auth/refresh/",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert (
            ok.headers.get("access-control-allow-origin") == origin
        ), "the website's origin is not allowed by CORS_ALLOWED_ORIGINS"
        assert (
            ok.headers.get("access-control-allow-credentials") == "true"
        ), "CORS does not allow credentials, so the sign-in cookie would not be sent"
        bad = self.request(
            "OPTIONS",
            "/api/v1/auth/refresh/",
            headers={"Origin": STRANGER, "Access-Control-Request-Method": "POST"},
        )
        assert bad.headers.get("access-control-allow-origin") in (
            None,
            "",
        ), "a stranger's origin is allowed by CORS"
        return "the website is allowed; a stranger is not"

    def check_admin_is_hidden(self) -> str:
        response = self.request("GET", "/admin/")
        assert (
            response.status_code == 404
        ), f"/admin/ answered {response.status_code}; set DJANGO_ADMIN_URL to something else"
        return "/admin/ is not there"

    def check_static_files(self) -> str:
        """Served by WhiteNoise from inside the container; the admin's own stylesheet is a
        reliable file to ask for."""
        response = self.request("GET", "/static/admin/css/base.css")
        assert (
            response.status_code == 200
        ), f"static files answered {response.status_code}; WhiteNoise or collectstatic is off"
        return "static files are served"

    def check_schema(self) -> str:
        response = self.request("GET", "/api/v1/schema/")
        assert response.status_code == 200, f"the API schema answered {response.status_code}"
        assert "openapi" in response.text[:200].lower(), "the schema isn't an OpenAPI document"
        return "the API schema is served"

    def check_unknown_status_page(self) -> str:
        response = self.request("GET", "/api/v1/public/status-pages/no-such-page-smoke-test/")
        assert (
            response.status_code == 404
        ), f"an unknown status page answered {response.status_code}"
        return "an unknown status page is a 404"

    def check_trigger_closed(self) -> str:
        unsigned = self.request("POST", "/internal/run-due-checks/")
        assert (
            unsigned.status_code == 401
        ), f"the scheduler trigger answered {unsigned.status_code} to an unsigned call, not 401"
        forged = self.request(
            "POST",
            "/internal/run-due-checks/",
            headers={"X-RootPulse-Signature": f"t={int(time.time())},v1={'0' * 64}"},
        )
        assert forged.status_code == 401, "the scheduler trigger accepted a forged signature"
        return "unsigned and forged calls get 401"

    def check_trigger_signed(self) -> str:
        if not self.secret:
            return "skipped (no SCHEDULER_SHARED_SECRET given)"
        timestamp = int(time.time())
        mac = hmac.new(self.secret.encode(), f"{timestamp}.".encode(), hashlib.sha256).hexdigest()
        began = time.monotonic()
        response = self.request(
            "POST",
            "/internal/run-due-checks/",
            headers={"X-RootPulse-Signature": f"t={timestamp},v1={mac}"},
            timeout=170,
        )
        took = time.monotonic() - began
        assert response.status_code == 200, (
            f"a correctly signed call answered {response.status_code}; "
            "is SCHEDULER_SHARED_SECRET the same on the API and here?"
        )
        body = response.json()
        for key in ("claimed", "providers", "notifications"):
            assert key in body, f"the pass summary has no '{key}'"
        assert body.get("errors", 0) == 0, f"the pass reported {body['errors']} errors"
        return f"one pass ran in {took:.1f}s (claimed {body['claimed']} monitor(s))"

    def check_site(self) -> str:
        if not self.site:
            return "skipped (no --site)"
        response = self.client.get(self.site + "/", headers={"User-Agent": "rootpulse-smoke-test"})
        assert response.status_code == 200, f"the website answered {response.status_code}"
        csp = response.headers.get("content-security-policy", "")
        assert "frame-ancestors 'none'" in csp, "the website sends no Content-Security-Policy"
        api_origin = f"{urlsplit(self.api).scheme}://{urlsplit(self.api).netloc}"
        assert (
            api_origin in csp or "connect-src 'self'" in csp
        ), "the website's content policy doesn't let the page call the API"
        return "the website is up, with its content policy"

    def check_site_login_page(self) -> str:
        if not self.site:
            return "skipped (no --site)"
        response = self.client.get(
            self.site + "/login",
            headers={"User-Agent": "rootpulse-smoke-test"},
            follow_redirects=True,
        )
        assert response.status_code == 200, f"/login answered {response.status_code}"
        return "the sign-in page loads"

    # --- run --------------------------------------------------------------------------------
    def run(self) -> bool:
        steps = [
            ("API is up", self.check_up),
            ("HTTPS is enforced", self.check_https_enforced),
            (
                "Anonymous access is refused, with headers",
                self.check_anonymous_refused_with_headers,
            ),
            ("CORS admits the website only", self.check_cors),
            ("Admin is not at /admin/", self.check_admin_is_hidden),
            ("Static files are served", self.check_static_files),
            ("API schema", self.check_schema),
            ("Unknown status page is a 404", self.check_unknown_status_page),
            ("Scheduler trigger is closed", self.check_trigger_closed),
            ("Scheduler trigger runs a pass", self.check_trigger_signed),
            ("Website is up", self.check_site),
            ("Sign-in page loads", self.check_site_login_page),
        ]
        for name, fn in steps:
            self.record(name, fn)
            # No point asking more questions of an API that isn't there.
            if name == "API is up" and not self.results[-1][0]:
                break
        return all(ok for ok, _, _ in self.results)

    def report(self) -> str:
        lines = []
        for ok, name, note in self.results:
            lines.append(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f": {note}" if note else ""))
        failed = sum(not ok for ok, _, _ in self.results)
        lines.append(f"\n{len(self.results) - failed} passed, {failed} failed")
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", required=True, help="the API's public address")
    parser.add_argument(
        "--site", help="the website's public address (adds the CORS and site checks)"
    )
    parser.add_argument(
        "--cors-origin",
        help="the Origin to test CORS with, when it isn't --site (a container on this machine)",
    )
    parser.add_argument(
        "--forwarded-https",
        action="store_true",
        help="pretend to be behind an HTTPS proxy (for a container on this machine)",
    )
    parser.add_argument(
        "--scheduler-secret",
        default=os.environ.get("SCHEDULER_SHARED_SECRET"),
        help="also run one real scheduler pass (default: $SCHEDULER_SHARED_SECRET)",
    )
    args = parser.parse_args(argv)

    smoke = Smoke(
        args.api,
        args.site,
        cors_origin=args.cors_origin,
        secret=args.scheduler_secret,
        forwarded_https=args.forwarded_https,
    )
    ok = smoke.run()
    print(f"Smoke test of {args.api}\n")
    print(smoke.report())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
