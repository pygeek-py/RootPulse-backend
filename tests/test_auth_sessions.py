from datetime import timedelta

import pytest
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import AccessToken

from .conftest import PASSWORD

ME = "/api/v1/auth/me/"
LOGIN = "/api/v1/auth/login/"
REFRESH = "/api/v1/auth/refresh/"
LOGOUT = "/api/v1/auth/logout/"

pytestmark = pytest.mark.django_db


def login(api, email):
    resp = api.post(LOGIN, {"email": email, "password": PASSWORD}, format="json")
    assert resp.status_code == 200
    return resp


class TestMe:
    def test_requires_authentication(self, api):
        assert api.get(ME).status_code == 401

    def test_rejects_garbage_and_expired_tokens(self, api, user):
        api.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt")
        assert api.get(ME).status_code == 401

        expired = AccessToken.for_user(user)
        expired.set_exp(lifetime=-timedelta(seconds=5))
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {expired}")
        assert api.get(ME).status_code == 401

    def test_returns_current_user_only(self, auth_api, user, make_user):
        make_user("someone-else@example.com")
        body = auth_api.get(ME).json()
        assert body["email"] == user.email
        assert body["has_password"] is True
        assert body["has_github"] is False
        assert set(body) == {"id", "email", "timezone", "has_password", "has_github", "created_at"}

    def test_can_update_timezone_but_not_email(self, auth_api, user):
        resp = auth_api.patch(
            ME, {"timezone": "Europe/Berlin", "email": "hijack@example.com"}, format="json"
        )
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.timezone == "Europe/Berlin"
        assert user.email == "alice@example.com"

    def test_rejects_unknown_timezone(self, auth_api):
        resp = auth_api.patch(ME, {"timezone": "Mars/Olympus"}, format="json")
        assert resp.status_code == 400
        assert resp.json()["field_errors"]["timezone"]


class TestRefresh:
    def test_rotates_the_cookie_and_returns_a_working_access_token(self, api, user):
        first = login(api, user.email).cookies["refresh_token"].value

        resp = api.post(REFRESH)
        assert resp.status_code == 200
        second = resp.cookies["refresh_token"].value
        assert second != first

        api.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access_token']}")
        assert api.get(ME).json()["email"] == user.email

    def test_a_used_refresh_token_cannot_be_replayed(self, api, user):
        old = login(api, user.email).cookies["refresh_token"].value
        assert api.post(REFRESH).status_code == 200  # legitimate rotation

        api.cookies["refresh_token"] = old  # an attacker replaying the stolen copy
        resp = api.post(REFRESH)
        assert resp.status_code == 401
        assert resp.cookies["refresh_token"].value == ""  # cookie is cleared

    def test_without_a_cookie(self, api):
        assert api.post(REFRESH).status_code == 401

    def test_with_a_garbage_cookie(self, api):
        api.cookies["refresh_token"] = "garbage"
        assert api.post(REFRESH).status_code == 401

    def test_for_a_deactivated_user(self, api, user):
        login(api, user.email)
        user.is_active = False
        user.save()
        assert api.post(REFRESH).status_code == 401

    @pytest.mark.parametrize("origin", ["https://evil.example", "null", None])
    def test_refuses_untrusted_origins(self, api, user, origin):
        """The CSRF defence for a cross-site (SameSite=None) cookie."""
        login(api, user.email)
        client = api
        client.defaults.pop("HTTP_ORIGIN", None)
        extra = {"HTTP_ORIGIN": origin} if origin else {}
        assert client.post(REFRESH, **extra).status_code == 403


class TestLogout:
    def test_revokes_the_session_and_clears_the_cookie(self, api, user):
        login(api, user.email)
        resp = api.post(LOGOUT)

        assert resp.status_code == 204
        assert resp.cookies["refresh_token"].value == ""
        assert BlacklistedToken.objects.count() == 1
        assert api.post(REFRESH).status_code == 401

    def test_is_idempotent_without_a_session(self, api):
        assert api.post(LOGOUT).status_code == 204

    def test_refuses_untrusted_origins(self, api, user):
        login(api, user.email)
        api.defaults["HTTP_ORIGIN"] = "https://evil.example"
        assert api.post(LOGOUT).status_code == 403
        assert BlacklistedToken.objects.count() == 0


class TestCookieAttributes:
    def test_production_settings_are_secure_and_cross_site_capable(self, api, user, settings):
        settings.AUTH_REFRESH_COOKIE_SECURE = True
        settings.AUTH_REFRESH_COOKIE_SAMESITE = "None"

        cookie = login(api, user.email).cookies["refresh_token"]
        assert cookie["secure"]
        assert cookie["samesite"] == "None"
        assert cookie["httponly"]
