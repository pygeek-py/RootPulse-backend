from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from accounts.models import AuditLog, EmailChallenge, User

from .conftest import emailed_credentials

REGISTER = "/api/v1/auth/register/"
LOGIN = "/api/v1/auth/login/"
VERIFY = "/api/v1/auth/verify/"

pytestmark = pytest.mark.django_db


def start(api, url, email):
    return api.post(url, {"email": email}, format="json")


class TestSignUp:
    def test_emails_a_link_and_a_code_but_creates_no_account_yet(self, api):
        resp = start(api, REGISTER, "New@Example.com")
        assert resp.status_code == 204
        assert not User.objects.exists()  # created only once the email is proven

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["new@example.com"]
        token, code = emailed_credentials()
        assert len(token) >= 40 and len(code) == 6

    def test_the_link_creates_the_account_and_signs_in(self, api):
        start(api, REGISTER, "new@example.com")
        token, _ = emailed_credentials()

        resp = api.post(VERIFY, {"token": token}, format="json")

        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["email"] == "new@example.com"
        assert body["access_token"]
        assert resp.cookies["refresh_token"]["httponly"]
        user = User.objects.get()
        assert not user.has_usable_password()
        assert AuditLog.objects.filter(action="register", user=user).exists()

    def test_the_code_works_too(self, api):
        start(api, REGISTER, "new@example.com")
        _, code = emailed_credentials()

        resp = api.post(VERIFY, {"email": "NEW@example.com", "code": code}, format="json")

        assert resp.status_code == 200
        assert User.objects.filter(email="new@example.com").exists()

    def test_signing_up_with_an_existing_email_just_signs_them_in(self, api, user, sign_in):
        start(api, REGISTER, user.email)
        token, _ = emailed_credentials()

        resp = api.post(VERIFY, {"token": token}, format="json")

        assert resp.status_code == 200
        assert User.objects.count() == 1
        assert AuditLog.objects.filter(action="login", user=user).exists()

    def test_rejects_a_malformed_email(self, api):
        assert start(api, REGISTER, "nope").status_code == 400
        assert not mail.outbox


class TestLogin:
    def test_existing_account_gets_an_email_and_can_sign_in(self, api, user):
        assert start(api, LOGIN, user.email).status_code == 204
        token, _ = emailed_credentials()

        resp = api.post(VERIFY, {"token": token}, format="json")

        assert resp.status_code == 200
        assert resp.json()["user"]["id"] == str(user.id)

    def test_unknown_email_gets_the_same_response_but_no_email(self, api):
        resp = start(api, LOGIN, "ghost@example.com")
        assert resp.status_code == 204
        assert not mail.outbox
        assert not EmailChallenge.objects.exists()

    def test_deactivated_accounts_get_nothing(self, api, make_user):
        user = make_user("off@example.com", is_active=False)
        assert start(api, LOGIN, user.email).status_code == 204
        assert not mail.outbox
        assert start(api, REGISTER, user.email).status_code == 204
        assert not mail.outbox


class TestSingleUseAndExpiry:
    def test_a_link_works_once(self, api, user):
        start(api, LOGIN, user.email)
        token, _ = emailed_credentials()
        assert api.post(VERIFY, {"token": token}, format="json").status_code == 200
        assert api.post(VERIFY, {"token": token}, format="json").status_code == 400

    def test_using_the_link_kills_the_code_and_vice_versa(self, api, user):
        start(api, LOGIN, user.email)
        token, code = emailed_credentials()
        assert api.post(VERIFY, {"token": token}, format="json").status_code == 200
        resp = api.post(VERIFY, {"email": user.email, "code": code}, format="json")
        assert resp.status_code == 400

    def test_a_newer_email_invalidates_the_older_one(self, api, user):
        start(api, LOGIN, user.email)
        old_token, old_code = emailed_credentials()
        start(api, LOGIN, user.email)
        new_token, _ = emailed_credentials()

        assert api.post(VERIFY, {"token": old_token}, format="json").status_code == 400
        resp = api.post(VERIFY, {"email": user.email, "code": old_code}, format="json")
        assert resp.status_code == 400
        assert api.post(VERIFY, {"token": new_token}, format="json").status_code == 200

    def test_expired_challenges_are_refused(self, api, user):
        start(api, LOGIN, user.email)
        token, code = emailed_credentials()
        EmailChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        assert api.post(VERIFY, {"token": token}, format="json").status_code == 400
        resp = api.post(VERIFY, {"email": user.email, "code": code}, format="json")
        assert resp.status_code == 400

    def test_an_unknown_token_is_refused_with_the_generic_error(self, api):
        resp = api.post(VERIFY, {"token": "nope"}, format="json")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "This link or code is invalid or has expired."


class TestCodeBruteForce:
    def test_the_code_dies_after_five_wrong_guesses(self, api, user):
        start(api, LOGIN, user.email)
        token, code = emailed_credentials()
        wrong = "000000" if code != "000000" else "111111"

        for _ in range(5):
            resp = api.post(VERIFY, {"email": user.email, "code": wrong}, format="json")
            assert resp.status_code == 400

        # Even the right code is refused now…
        resp = api.post(VERIFY, {"email": user.email, "code": code}, format="json")
        assert resp.status_code == 400
        # …but the link from the same email still works.
        assert api.post(VERIFY, {"token": token}, format="json").status_code == 200

    def test_a_wrong_guess_for_someone_elses_email_does_nothing(self, api, user):
        resp = api.post(VERIFY, {"email": "ghost@example.com", "code": "123456"}, format="json")
        assert resp.status_code == 400

    @pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", ""])
    def test_malformed_codes_are_rejected_up_front(self, api, user, code):
        resp = api.post(VERIFY, {"email": user.email, "code": code}, format="json")
        assert resp.status_code == 400

    def test_needs_either_a_token_or_email_and_code(self, api):
        assert api.post(VERIFY, {}, format="json").status_code == 400
        assert api.post(VERIFY, {"email": "a@example.com"}, format="json").status_code == 400


class TestHygiene:
    def test_only_hashes_are_stored(self, api, user):
        start(api, LOGIN, user.email)
        token, code = emailed_credentials()
        challenge = EmailChallenge.objects.get()
        assert token not in (challenge.link_hash, challenge.code_hash)
        assert code not in challenge.code_hash
        assert len(challenge.link_hash) == 64

    def test_resend_cooldown_sends_one_email_per_minute_without_changing_the_response(
        self, api, user, settings
    ):
        settings.EMAIL_CHALLENGE_RESEND_SECONDS = 60
        assert start(api, LOGIN, user.email).status_code == 204
        assert start(api, LOGIN, user.email).status_code == 204
        assert len(mail.outbox) == 1

    def test_the_cooldown_does_not_block_signing_in_again_after_using_an_email(
        self, api, user, settings
    ):
        settings.EMAIL_CHALLENGE_RESEND_SECONDS = 60
        start(api, LOGIN, user.email)
        token, _ = emailed_credentials()
        assert api.post(VERIFY, {"token": token}, format="json").status_code == 200

        start(api, LOGIN, user.email)  # signed out and back in within the minute

        assert len(mail.outbox) == 2

    def test_email_failures_do_not_change_the_response(self, api, user, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("smtp down")

        monkeypatch.setattr("accounts.passwordless.send_mail", boom)
        assert start(api, LOGIN, user.email).status_code == 204

    def test_the_email_endpoints_are_rate_limited(self, api, monkeypatch):
        from rest_framework.settings import api_settings
        from rest_framework.throttling import ScopedRateThrottle

        rates = {**api_settings.DEFAULT_THROTTLE_RATES, "auth_email_start": "3/hour"}
        monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", rates)

        codes = [start(api, LOGIN, f"x{i}@example.com").status_code for i in range(5)]

        assert codes[:3] == [204, 204, 204]
        assert codes[3] == 429

    def test_no_password_endpoints_remain(self):
        from django.urls import Resolver404, resolve

        for path in ("/api/v1/auth/password/forgot/", "/api/v1/auth/password/reset/"):
            with pytest.raises(Resolver404):
                resolve(path)
