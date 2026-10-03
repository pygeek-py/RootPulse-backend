import re

import pytest
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import User

ORIGIN = "http://localhost:3000"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    # Throttle counters live in the cache; without this a test that hits the
    # login limit would poison every test after it.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    """An API client that, like the real frontend, sends an allowed Origin."""
    return APIClient(HTTP_ORIGIN=ORIGIN)


@pytest.fixture
def make_user(db):
    counter = {"n": 0}

    def _make(email: str | None = None, **extra) -> User:
        counter["n"] += 1
        email = email or f"user{counter['n']}@example.com"
        user = User(username=f"u{counter['n']}-{email}", email=email, **extra)
        user.set_unusable_password()  # sign-in is passwordless
        user.save()
        return user

    return _make


@pytest.fixture
def user(make_user):
    return make_user("alice@example.com")


def emailed_credentials() -> tuple[str, str]:
    """(link token, 6-digit code) from the most recent sign-in email."""
    body = mail.outbox[-1].body
    token = re.search(r"/auth/verify#token=([\w-]+)", body).group(1)
    code = re.search(r"code: (\d{6})", body).group(1)
    return token, code


@pytest.fixture
def sign_in():
    """Runs the real passwordless flow (request email, redeem the code) and
    returns the verify response; the client then holds the refresh cookie."""

    def _sign_in(api, email: str):
        assert api.post("/api/v1/auth/login/", {"email": email}, format="json").status_code == 204
        _, code = emailed_credentials()
        resp = api.post("/api/v1/auth/verify/", {"email": email, "code": code}, format="json")
        assert resp.status_code == 200
        return resp

    return _sign_in


@pytest.fixture
def auth_api(api, user, sign_in):
    """An API client already signed in as `user` (Bearer access token)."""
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {sign_in(api, user.email).json()['access_token']}")
    return api
