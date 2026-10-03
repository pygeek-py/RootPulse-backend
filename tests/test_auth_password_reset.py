import re

import pytest
from django.core import mail

from accounts.models import AuditLog

from .conftest import PASSWORD

FORGOT = "/api/v1/auth/password/forgot/"
RESET = "/api/v1/auth/password/reset/"
LOGIN = "/api/v1/auth/login/"
REFRESH = "/api/v1/auth/refresh/"
NEW_PASSWORD = "a-brand-new-passphrase-7"

pytestmark = pytest.mark.django_db


def request_token(api, email) -> str:
    assert api.post(FORGOT, {"email": email}, format="json").status_code == 204
    link = re.search(r"/reset-password/(\S+)", mail.outbox[-1].body).group(1)
    return link


class TestForgot:
    def test_sends_a_reset_link_to_a_real_account(self, api, user):
        assert api.post(FORGOT, {"email": user.email}, format="json").status_code == 204

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [user.email]
        assert "http://localhost:3000/reset-password/" in mail.outbox[0].body
        assert AuditLog.objects.filter(action="password_reset_requested", user=user).exists()

    def test_responds_identically_for_unknown_emails_and_sends_nothing(self, api, user):
        known = api.post(FORGOT, {"email": user.email}, format="json")
        unknown = api.post(FORGOT, {"email": "ghost@example.com"}, format="json")

        assert known.status_code == unknown.status_code == 204
        assert known.content == unknown.content  # nothing to enumerate accounts with
        assert len(mail.outbox) == 1

    def test_email_is_case_insensitive(self, api, user):
        api.post(FORGOT, {"email": user.email.upper()}, format="json")
        assert len(mail.outbox) == 1

    def test_rejects_malformed_email(self, api):
        assert api.post(FORGOT, {"email": "nope"}, format="json").status_code == 400

    def test_is_rate_limited(self, api, user):
        statuses = [
            api.post(FORGOT, {"email": user.email}, format="json").status_code for _ in range(6)
        ]
        assert statuses == [204] * 5 + [429]


class TestReset:
    def test_sets_the_new_password(self, api, user):
        token = request_token(api, user.email)

        resp = api.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json")
        assert resp.status_code == 204

        assert (
            api.post(
                LOGIN, {"email": user.email, "password": NEW_PASSWORD}, format="json"
            ).status_code
            == 200
        )
        api.cookies.clear()
        assert (
            api.post(LOGIN, {"email": user.email, "password": PASSWORD}, format="json").status_code
            == 401
        )
        assert AuditLog.objects.filter(action="password_reset", user=user).exists()

    def test_a_link_works_only_once(self, api, user):
        token = request_token(api, user.email)
        assert (
            api.post(
                RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json"
            ).status_code
            == 204
        )

        again = api.post(
            RESET, {"token": token, "new_password": "another-passphrase-8"}, format="json"
        )
        assert again.status_code == 400

    @pytest.mark.parametrize("token", ["garbage", "", "abc.def", "....", "MQ.token-that-is-wrong"])
    def test_invalid_tokens_are_rejected(self, api, user, token):
        resp = api.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json")
        assert resp.status_code in (400,)
        user.refresh_from_db()
        assert user.check_password(PASSWORD)

    def test_a_token_for_one_user_cannot_reset_another(self, api, user, make_user):
        other = make_user("bob@example.com")
        uid_other, _, _ = request_token(api, other.email).partition(".")
        _, _, token_alice = request_token(api, user.email).partition(".")

        resp = api.post(
            RESET,
            {"token": f"{uid_other}.{token_alice}", "new_password": NEW_PASSWORD},
            format="json",
        )
        assert resp.status_code == 400
        other.refresh_from_db()
        assert other.check_password(PASSWORD)

    def test_new_password_must_meet_the_policy(self, api, user):
        token = request_token(api, user.email)
        resp = api.post(RESET, {"token": token, "new_password": "short"}, format="json")
        assert resp.status_code == 400
        assert resp.json()["field_errors"]["new_password"]

    def test_signs_out_every_existing_session(self, api, user):
        api.post(LOGIN, {"email": user.email, "password": PASSWORD}, format="json")  # has a session
        token = request_token(api, user.email)
        api.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json")

        assert api.post(REFRESH).status_code == 401
