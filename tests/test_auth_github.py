import httpx
import pytest

from accounts import github
from accounts.models import AuditLog, User

REDIRECT = "/api/v1/auth/github/redirect/"
CALLBACK = "/api/v1/auth/github/callback/"
FRONTEND = "http://localhost:3000"

pytestmark = pytest.mark.django_db


def fake_github(monkeypatch, *, token="gho_token", user_id=777, emails=None, token_status=200):
    """Replace GitHub's HTTP API with an in-process fake."""
    if emails is None:
        emails = [{"email": "Octo@Example.com", "primary": True, "verified": True}]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(github.TOKEN_URL):
            return httpx.Response(token_status, json={"access_token": token} if token else {})
        assert request.headers["authorization"] == f"Bearer {token}"
        if url.endswith("/user/emails"):
            return httpx.Response(200, json=emails)
        return httpx.Response(200, json={"id": user_id})

    real_client = httpx.Client
    monkeypatch.setattr(
        "accounts.github.httpx.Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )


def start_flow(api) -> str:
    resp = api.get(REDIRECT)
    return resp.cookies["gh_oauth_state"].value


class TestRedirect:
    def test_sends_the_user_to_github_with_a_state_cookie(self, api):
        resp = api.get(REDIRECT)

        assert resp.status_code == 302
        assert resp["Location"].startswith("https://github.com/login/oauth/authorize?")
        state = resp.cookies["gh_oauth_state"]
        assert f"state={state.value}" in resp["Location"]
        assert "client_id=test-client-id" in resp["Location"]
        assert state["httponly"]
        assert state["samesite"] == "Lax"  # GitHub comes back via a cross-site GET

    def test_each_attempt_gets_a_fresh_state(self, api):
        assert start_flow(api) != start_flow(api)

    def test_when_not_configured_it_bounces_back_politely(self, api, settings):
        settings.GITHUB_OAUTH_CLIENT_ID = ""
        resp = api.get(REDIRECT)
        assert resp.status_code == 302
        assert resp["Location"] == f"{FRONTEND}/login?error=github_not_configured"


class TestCallback:
    def test_creates_an_account_and_starts_a_session(self, api, monkeypatch):
        fake_github(monkeypatch)
        state = start_flow(api)

        resp = api.get(CALLBACK, {"code": "abc", "state": state})

        assert resp.status_code == 302
        assert resp["Location"] == f"{FRONTEND}/auth/callback"
        assert resp.cookies["refresh_token"]["httponly"]
        user = User.objects.get(github_id="777")
        assert user.email == "octo@example.com"
        assert not user.has_usable_password()
        assert AuditLog.objects.filter(action="login", user=user).exists()

    def test_links_to_an_existing_account_with_the_same_verified_email(
        self, api, make_user, monkeypatch
    ):
        existing = make_user("octo@example.com")
        fake_github(monkeypatch)
        state = start_flow(api)

        api.get(CALLBACK, {"code": "abc", "state": state})

        existing.refresh_from_db()
        assert existing.github_id == "777"
        assert User.objects.count() == 1
        assert existing.has_usable_password()  # password login keeps working
        assert AuditLog.objects.filter(action="github_linked", user=existing).exists()

    def test_an_existing_github_id_wins_even_if_the_email_changed(
        self, api, make_user, monkeypatch
    ):
        linked = make_user("old@example.com", github_id="777")
        fake_github(monkeypatch)
        state = start_flow(api)

        resp = api.get(CALLBACK, {"code": "abc", "state": state})

        assert resp["Location"] == f"{FRONTEND}/auth/callback"
        assert User.objects.count() == 1
        assert AuditLog.objects.filter(action="login", user=linked).exists()

    def test_ignores_unverified_emails(self, api, monkeypatch):
        fake_github(
            monkeypatch,
            emails=[{"email": "victim@example.com", "primary": True, "verified": False}],
        )
        state = start_flow(api)

        resp = api.get(CALLBACK, {"code": "abc", "state": state})

        assert resp["Location"] == f"{FRONTEND}/login?error=github_failed"
        assert not User.objects.exists()

    @pytest.mark.parametrize("cookie_state", ["", "something-else"], ids=["no-cookie", "mismatch"])
    def test_rejects_a_bad_state_without_calling_github(self, api, monkeypatch, cookie_state):
        def boom(code):
            raise AssertionError("must not contact GitHub when state is invalid")

        monkeypatch.setattr(github, "fetch_profile", boom)
        if cookie_state:
            api.cookies["gh_oauth_state"] = cookie_state

        resp = api.get(CALLBACK, {"code": "abc", "state": "attacker-chosen"})

        assert resp["Location"] == f"{FRONTEND}/login?error=github_state"
        assert not User.objects.exists()
        assert "refresh_token" not in resp.cookies

    def test_user_denying_access(self, api, monkeypatch):
        fake_github(monkeypatch)
        state = start_flow(api)
        resp = api.get(CALLBACK, {"error": "access_denied", "state": state})
        assert resp["Location"] == f"{FRONTEND}/login?error=github_denied"

    def test_github_failure_is_reported_without_leaking_details(self, api, monkeypatch):
        fake_github(monkeypatch, token_status=500)
        state = start_flow(api)
        resp = api.get(CALLBACK, {"code": "abc", "state": state})
        assert resp["Location"] == f"{FRONTEND}/login?error=github_failed"

    def test_deactivated_accounts_cannot_sign_in_via_github(self, api, make_user, monkeypatch):
        make_user("octo@example.com", github_id="777", is_active=False)
        fake_github(monkeypatch)
        state = start_flow(api)
        resp = api.get(CALLBACK, {"code": "abc", "state": state})
        assert resp["Location"] == f"{FRONTEND}/login?error=account_disabled"
        assert "refresh_token" not in resp.cookies


class TestProfileFetching:
    def test_prefers_the_primary_verified_email(self, monkeypatch):
        fake_github(
            monkeypatch,
            emails=[
                {"email": "other@example.com", "primary": False, "verified": True},
                {"email": "Main@Example.com", "primary": True, "verified": True},
            ],
        )
        profile = github.fetch_profile("abc")
        assert (profile.github_id, profile.email) == ("777", "main@example.com")

    def test_falls_back_to_any_verified_email(self, monkeypatch):
        fake_github(
            monkeypatch, emails=[{"email": "x@example.com", "primary": False, "verified": True}]
        )
        assert github.fetch_profile("abc").email == "x@example.com"

    def test_missing_access_token(self, monkeypatch):
        fake_github(monkeypatch, token=None)
        with pytest.raises(github.GitHubError):
            github.fetch_profile("abc")
