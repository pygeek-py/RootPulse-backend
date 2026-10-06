"""SIGNUPS_OPEN=false: a public deployment of a single-owner product stops taking new accounts
once its owner has one. People who already have an account sign in as before."""

import pytest
from django.core import mail

from accounts.models import AuditLog, EmailChallenge, User

from .conftest import emailed_credentials
from .test_auth_github import CALLBACK, FRONTEND, fake_github, start_flow

REGISTER = "/api/v1/auth/register/"
LOGIN = "/api/v1/auth/login/"
VERIFY = "/api/v1/auth/verify/"

pytestmark = pytest.mark.django_db


@pytest.fixture
def closed(settings):
    settings.SIGNUPS_OPEN = False


def post(api, url, email):
    return api.post(url, {"email": email}, format="json")


class TestSignupsAreOpenByDefault:
    def test_the_default_is_open(self):
        from config import settings as prod_settings

        assert prod_settings.SIGNUPS_OPEN is True

    def test_a_new_address_can_register(self, api):
        post(api, REGISTER, "new@example.com")
        assert len(mail.outbox) == 1


class TestClosedEmailSignup:
    def test_a_stranger_gets_the_same_answer_and_no_email(self, api, closed):
        resp = post(api, REGISTER, "stranger@example.com")
        assert resp.status_code == 204  # identical to an open instance: nothing to learn
        assert mail.outbox == []
        assert not EmailChallenge.objects.exists()

    def test_nothing_distinguishes_closed_from_an_email_that_failed_to_send(self, api, closed):
        closed_answer = post(api, REGISTER, "stranger@example.com")
        assert closed_answer.content == b""

    def test_an_existing_owner_can_still_sign_in(self, api, closed, make_user):
        make_user(email="owner@example.com")
        assert post(api, LOGIN, "owner@example.com").status_code == 204
        assert len(mail.outbox) == 1
        token, _ = emailed_credentials()
        resp = api.post(VERIFY, {"token": token}, format="json")
        assert resp.status_code == 200
        assert resp.json()["user"]["email"] == "owner@example.com"

    def test_the_register_form_still_signs_the_owner_in(self, api, closed, make_user):
        make_user(email="owner@example.com")
        post(api, REGISTER, "owner@example.com")
        assert len(mail.outbox) == 1

    def test_a_link_sent_before_signups_closed_cannot_create_an_account(self, api, settings):
        post(api, REGISTER, "late@example.com")
        token, _ = emailed_credentials()
        settings.SIGNUPS_OPEN = False

        resp = api.post(VERIFY, {"token": token}, format="json")

        assert resp.status_code == 400
        assert not User.objects.exists()

    def test_the_code_path_is_closed_too(self, api, settings):
        post(api, REGISTER, "late@example.com")
        _, code = emailed_credentials()
        settings.SIGNUPS_OPEN = False

        resp = api.post(VERIFY, {"email": "late@example.com", "code": code}, format="json")

        assert resp.status_code == 400
        assert not User.objects.exists()


class TestClosedGitHubSignup:
    def test_a_new_github_account_is_turned_away(self, api, closed, monkeypatch):
        fake_github(monkeypatch)
        state = start_flow(api)
        api.cookies["gh_oauth_state"] = state

        resp = api.get(CALLBACK, {"state": state, "code": "abc"})

        assert resp.status_code == 302
        assert resp["Location"] == f"{FRONTEND}/login?error=signups_closed"
        assert "refresh_token" not in resp.cookies
        assert not User.objects.exists()
        assert AuditLog.objects.filter(action="register_refused").exists()

    def test_the_owner_signs_in_with_github_as_before(self, api, closed, make_user, monkeypatch):
        make_user(email="octo@example.com")
        fake_github(monkeypatch)
        state = start_flow(api)
        api.cookies["gh_oauth_state"] = state

        resp = api.get(CALLBACK, {"state": state, "code": "abc"})

        assert resp["Location"] == f"{FRONTEND}/auth/callback"
        assert User.objects.count() == 1
