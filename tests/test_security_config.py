"""Guards for security settings that are easy to weaken by accident."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model

from config import settings as prod_settings


def test_production_hashes_passwords_with_argon2():
    # config.settings_test swaps in MD5 for speed; production must not.
    assert prod_settings.PASSWORD_HASHERS[0].endswith("Argon2PasswordHasher")


@pytest.mark.django_db
def test_argon2_hashes_are_actually_produced(settings):
    settings.PASSWORD_HASHERS = prod_settings.PASSWORD_HASHERS
    user = get_user_model().objects.create_user(
        username="a", email="a@example.com", password="x" * 12
    )
    assert user.password.startswith("argon2$")


def test_password_policy_requires_ten_characters():
    min_length = next(
        v["OPTIONS"]["min_length"]
        for v in prod_settings.AUTH_PASSWORD_VALIDATORS
        if v["NAME"].endswith("MinimumLengthValidator")
    )
    assert min_length >= 10


def test_token_lifetimes_and_rotation():
    jwt = prod_settings.SIMPLE_JWT
    assert jwt["ACCESS_TOKEN_LIFETIME"] <= timedelta(minutes=15)
    assert jwt["REFRESH_TOKEN_LIFETIME"] <= timedelta(days=30)
    assert jwt["ROTATE_REFRESH_TOKENS"] and jwt["BLACKLIST_AFTER_ROTATION"]


def test_api_is_private_by_default_and_cors_has_no_wildcard():
    rest = prod_settings.REST_FRAMEWORK
    assert "rest_framework.permissions.IsAuthenticated" in rest["DEFAULT_PERMISSION_CLASSES"]
    assert "*" not in prod_settings.CORS_ALLOWED_ORIGINS
    assert not getattr(prod_settings, "CORS_ALLOW_ALL_ORIGINS", False)


def test_every_auth_endpoint_has_its_own_rate_limit():
    rates = prod_settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    for scope in (
        "auth_email_start",
        "auth_verify",
        "auth_refresh",
    ):
        assert scope in rates


def test_openapi_schema_documents_the_auth_api(client):
    resp = client.get("/api/v1/schema/", HTTP_ACCEPT="application/json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "/api/v1/auth/verify/" in schema["paths"]
    assert "/api/v1/auth/me/" in schema["paths"]
    assert "jwtAuth" in schema["components"]["securitySchemes"]
