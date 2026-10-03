import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import User

PASSWORD = "correct-horse-battery-9"
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

    def _make(email: str | None = None, password: str | None = PASSWORD, **extra) -> User:
        counter["n"] += 1
        email = email or f"user{counter['n']}@example.com"
        user = User(username=f"u{counter['n']}-{email}", email=email, **extra)
        if password is None:
            user.set_unusable_password()
        else:
            user.set_password(password)
        user.save()
        return user

    return _make


@pytest.fixture
def user(make_user):
    return make_user("alice@example.com")


@pytest.fixture
def auth_api(api, user):
    """An API client already logged in as `user` (Bearer access token)."""
    resp = api.post(
        "/api/v1/auth/login/", {"email": user.email, "password": PASSWORD}, format="json"
    )
    assert resp.status_code == 200
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access_token']}")
    return api
