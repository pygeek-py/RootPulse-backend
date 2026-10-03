import pytest

from accounts.models import AuditLog, User

from .conftest import PASSWORD

REGISTER = "/api/v1/auth/register/"
LOGIN = "/api/v1/auth/login/"

pytestmark = pytest.mark.django_db


class TestRegister:
    def test_creates_account_and_returns_session(self, api):
        resp = api.post(REGISTER, {"email": "New@Example.com", "password": PASSWORD}, format="json")

        assert resp.status_code == 201
        body = resp.json()
        assert body["user"]["email"] == "new@example.com"  # normalised
        assert body["access_token"]
        assert "password" not in body["user"]  # no hash, no plaintext

        cookie = resp.cookies["refresh_token"]
        assert cookie["httponly"]
        assert cookie["path"] == "/api/v1/auth/"
        assert cookie.value  # the refresh token is in the cookie, never the body
        assert cookie.value not in str(body)

    def test_password_is_hashed_not_stored(self, api):
        api.post(REGISTER, {"email": "a@example.com", "password": PASSWORD}, format="json")
        stored = User.objects.get(email="a@example.com")
        assert stored.password != PASSWORD
        assert stored.check_password(PASSWORD)

    def test_duplicate_email_is_409_case_insensitively(self, api, user):
        resp = api.post(
            REGISTER, {"email": user.email.upper(), "password": PASSWORD}, format="json"
        )
        assert resp.status_code == 409
        assert resp.json()["field_errors"]["email"]
        assert User.objects.count() == 1

    @pytest.mark.parametrize(
        "password",
        ["short1", "1234567890123", "qwertyuiop"],
        ids=["too-short", "all-numeric", "common"],
    )
    def test_weak_passwords_are_rejected_on_the_password_field(self, api, password):
        resp = api.post(REGISTER, {"email": "w@example.com", "password": password}, format="json")
        assert resp.status_code == 400
        assert resp.json()["field_errors"]["password"]
        assert not User.objects.filter(email="w@example.com").exists()

    def test_password_too_similar_to_email_is_rejected(self, api):
        resp = api.post(
            REGISTER, {"email": "johnsmith@example.com", "password": "johnsmith123"}, format="json"
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "payload", [{}, {"email": "x"}, {"email": "not-an-email", "password": PASSWORD}]
    )
    def test_invalid_input(self, api, payload):
        resp = api.post(REGISTER, payload, format="json")
        assert resp.status_code == 400
        assert "detail" in resp.json()

    def test_is_audited(self, api):
        api.post(REGISTER, {"email": "a@example.com", "password": PASSWORD}, format="json")
        assert AuditLog.objects.filter(action="register", user__email="a@example.com").exists()

    def test_registration_is_rate_limited(self, api):
        statuses = [
            api.post(
                REGISTER, {"email": f"u{i}@example.com", "password": PASSWORD}, format="json"
            ).status_code
            for i in range(11)
        ]
        assert statuses[:10] == [201] * 10
        assert statuses[10] == 429


class TestLogin:
    def test_success(self, api, user):
        resp = api.post(LOGIN, {"email": user.email, "password": PASSWORD}, format="json")
        assert resp.status_code == 200
        assert resp.json()["user"]["email"] == user.email
        assert resp.json()["access_token"]
        assert resp.cookies["refresh_token"]["httponly"]

    def test_email_is_case_insensitive(self, api, user):
        resp = api.post(LOGIN, {"email": "ALICE@Example.com", "password": PASSWORD}, format="json")
        assert resp.status_code == 200

    def test_wrong_password_and_unknown_email_look_identical(self, api, user):
        wrong_pw = api.post(
            LOGIN, {"email": user.email, "password": "nope-nope-nope"}, format="json"
        )
        unknown = api.post(
            LOGIN, {"email": "ghost@example.com", "password": PASSWORD}, format="json"
        )

        assert wrong_pw.status_code == unknown.status_code == 401
        assert wrong_pw.json() == unknown.json()  # nothing to enumerate accounts with
        assert "refresh_token" not in wrong_pw.cookies

    def test_inactive_account_cannot_log_in(self, api, make_user):
        inactive = make_user("gone@example.com", is_active=False)
        resp = api.post(LOGIN, {"email": inactive.email, "password": PASSWORD}, format="json")
        assert resp.status_code == 401

    def test_github_only_account_has_no_password_login(self, api, make_user):
        gh = make_user("gh@example.com", password=None, github_id="42")
        for guess in ("", PASSWORD, "!"):
            resp = api.post(LOGIN, {"email": gh.email, "password": guess}, format="json")
            assert resp.status_code in (400, 401)

    def test_failed_and_successful_logins_are_audited_without_secrets(self, api, user):
        api.post(LOGIN, {"email": user.email, "password": "wrong-wrong-wrong"}, format="json")
        api.post(LOGIN, {"email": user.email, "password": PASSWORD}, format="json")

        failed = AuditLog.objects.get(action="login_failed")
        assert failed.metadata == {"email": user.email}
        assert "wrong-wrong-wrong" not in str(failed.metadata)
        assert AuditLog.objects.filter(action="login", user=user).exists()

    def test_login_is_rate_limited(self, api, user):
        bad = {"email": user.email, "password": "wrong-wrong-wrong"}
        statuses = [api.post(LOGIN, bad, format="json").status_code for _ in range(6)]
        assert statuses == [401] * 5 + [429]

    def test_rate_limit_cannot_be_dodged_by_spoofing_forwarded_for(self, api, user):
        bad = {"email": user.email, "password": "wrong-wrong-wrong"}
        statuses = [
            api.post(LOGIN, bad, format="json", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}").status_code
            for i in range(6)
        ]
        assert statuses[-1] == 429
