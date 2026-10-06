"""scripts/smoke_test.py: the post-deploy checker. Its logic is tested two ways: against a
simulated deployment that is right (all pass) or wrong in one specific way (that check fails),
and against the real application in-process, so the script and the API can't drift apart."""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from django.core.wsgi import get_wsgi_application

_SPEC = importlib.util.spec_from_file_location(
    "smoke_test", Path(__file__).resolve().parent.parent / "scripts" / "smoke_test.py"
)
smoke_test = importlib.util.module_from_spec(_SPEC)
sys.modules["smoke_test"] = smoke_test
_SPEC.loader.exec_module(smoke_test)

SITE = "https://app.example.com"
API = "https://api.example.com"
SECURE_HEADERS = {
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "strict-transport-security": "max-age=31536000",
}


def deployment(**broken):
    """A fake deployment that behaves correctly except where `broken` says otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        if url.scheme == "http":
            return httpx.Response(301, headers={"location": "https://" + url.host + path})
        if path == "/health/":
            return httpx.Response(broken.get("health", 200), json={"status": "ok"})
        if path == "/api/v1/monitors/":
            return httpx.Response(
                broken.get("anonymous", 401),
                headers={
                    k: v for k, v in SECURE_HEADERS.items() if k not in broken.get("drop", ())
                },
            )
        if path == "/api/v1/auth/refresh/" and request.method == "OPTIONS":
            origin = request.headers.get("origin")
            allowed = origin == SITE or broken.get("cors") == "open"
            if broken.get("cors") == "closed":
                allowed = False
            headers = {}
            if allowed:
                headers = {
                    "access-control-allow-origin": origin,
                    "access-control-allow-credentials": "true",
                }
            return httpx.Response(200, headers=headers)
        if path == "/smoke-test-no-such-page/":
            if broken.get("debug"):
                return httpx.Response(
                    404, text="<p>DEBUG = True</p> Using the URLconf defined in x"
                )
            return httpx.Response(404, text="Not Found")
        if path == "/admin/":
            return httpx.Response(broken.get("admin", 404))
        if path == "/static/admin/css/base.css":
            return httpx.Response(broken.get("static", 200))
        if path == "/api/v1/schema/":
            return httpx.Response(200, text="openapi: 3.0.3\ninfo: {}")
        if path.startswith("/api/v1/public/status-pages/"):
            return httpx.Response(broken.get("status_page", 404))
        if path == "/internal/run-due-checks/":
            signature = request.headers.get("x-rootpulse-signature", "")
            if broken.get("trigger") == "open" or (
                "v1=" in signature and "0" * 64 not in signature
            ):
                return httpx.Response(
                    200, json={"claimed": 2, "errors": 0, "providers": {}, "notifications": {}}
                )
            return httpx.Response(401)
        if url.host == "app.example.com":
            if path == "/login":
                return httpx.Response(200)
            return httpx.Response(
                broken.get("site", 200),
                headers={
                    "content-security-policy": "default-src 'self'; connect-src 'self' "
                    + API
                    + "; frame-ancestors 'none'"
                },
            )
        return httpx.Response(404)

    return handler


def run(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    s = smoke_test.Smoke(API, SITE, client=client, cold_start_seconds=0, **kwargs)
    ok = s.run()
    return ok, {name: (passed, note) for passed, name, note in s.results}


def test_a_correct_deployment_passes_every_check():
    ok, results = run(deployment(), secret="s" * 40)
    assert ok, results
    assert all(passed for passed, _ in results.values())
    assert "claimed 2" in results["Scheduler trigger runs a pass"][1]


@pytest.mark.parametrize(
    ("broken", "check"),
    [
        ({"anonymous": 200}, "Anonymous access is refused, with headers"),
        ({"drop": ("strict-transport-security",)}, "Anonymous access is refused, with headers"),
        ({"drop": ("cache-control",)}, "Anonymous access is refused, with headers"),
        ({"drop": ("x-content-type-options",)}, "Anonymous access is refused, with headers"),
        ({"cors": "open"}, "CORS admits the website only"),
        ({"cors": "closed"}, "CORS admits the website only"),
        ({"admin": 200}, "Admin is not at /admin/"),
        ({"debug": True}, "Debug mode is off"),
        ({"static": 404}, "Static files are served"),
        ({"status_page": 200}, "Unknown status page is a 404"),
        ({"trigger": "open"}, "Scheduler trigger is closed"),
        ({"site": 502}, "Website is up"),
    ],
)
def test_each_kind_of_misconfiguration_is_caught(broken, check):
    ok, results = run(deployment(**broken), secret="s" * 40)
    assert not ok
    assert results[check][0] is False, results[check]


def test_an_api_that_never_comes_up_stops_the_run_early():
    ok, results = run(deployment(health=503))
    assert not ok
    assert list(results) == ["API is up"]


def test_plain_http_that_does_not_redirect_is_caught():
    def handler(request):
        if request.url.scheme == "http":
            return httpx.Response(200)
        return deployment()(request)

    ok, results = run(handler)
    assert results["HTTPS is enforced"][0] is False


def test_a_wrong_scheduler_secret_is_reported_as_such():
    def handler(request):
        if request.url.path == "/internal/run-due-checks/":
            return httpx.Response(401)
        return deployment()(request)

    ok, results = run(handler, secret="wrong" * 10)
    passed, note = results["Scheduler trigger runs a pass"]
    assert not passed
    assert "SCHEDULER_SHARED_SECRET" in note


def test_the_trigger_is_skipped_without_a_secret():
    ok, results = run(deployment())
    assert ok
    assert "skipped" in results["Scheduler trigger runs a pass"][1]


def test_the_report_and_exit_status(capsys, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        smoke_test.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(deployment()), **kw),
    )
    assert smoke_test.main(["--api", API, "--site", SITE]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "0 failed" in out

    monkeypatch.setattr(
        smoke_test.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(deployment(admin=200)), **kw),
    )
    assert smoke_test.main(["--api", API, "--site", SITE]) == 1


# --- against the real application ---------------------------------------------------------------


@pytest.fixture
def real():
    client = httpx.Client(
        transport=httpx.WSGITransport(app=get_wsgi_application()),
        base_url="http://testserver",
        follow_redirects=False,
    )
    # The test settings serve plain http and have no static files collected, so the checks that
    # are about production mode (HSTS, https redirect, static) are not asked of this one.
    return smoke_test.Smoke(
        "http://testserver", "http://localhost:3000", secret="test-scheduler-secret", client=client
    )


@pytest.mark.django_db
def test_the_real_api_agrees_with_what_the_script_expects(real):
    for check in (
        real.check_up,
        real.check_anonymous_refused_with_headers,
        real.check_cors,
        real.check_schema,
        real.check_unknown_status_page,
        real.check_trigger_closed,
        real.check_trigger_signed,
    ):
        real.record(check.__name__, check)
    failed = [(name, note) for ok, name, note in real.results if not ok]
    assert failed == []


def test_cors_can_be_checked_without_a_website_to_fetch():
    client = httpx.Client(transport=httpx.MockTransport(deployment()), follow_redirects=False)
    s = smoke_test.Smoke(API, None, cors_origin=SITE, client=client, cold_start_seconds=0)
    s.record("cors", s.check_cors)
    assert s.results[0][0], s.results
    s.record("site", s.check_site)  # with no website, the site checks are skipped, not failed
    assert s.results[1][0] and "skipped" in s.results[1][2]
