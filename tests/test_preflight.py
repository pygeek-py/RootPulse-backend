"""scripts/preflight.py: the production-environment checker. A pure function over a mapping, so
every rule is tested with plain dicts; and the report must never contain a value."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).resolve().parent.parent / "scripts" / "preflight.py"
)
preflight = importlib.util.module_from_spec(_SPEC)
sys.modules["preflight"] = preflight  # dataclasses looks its module up by name
_SPEC.loader.exec_module(preflight)

SECRET = "s3cr3t-" + "k" * 60

GOOD = {
    "DJANGO_DEBUG": "false",
    "DJANGO_SECRET_KEY": SECRET,
    "DJANGO_ALLOWED_HOSTS": "api.example.com",
    "DJANGO_ADMIN_URL": "console-8f3k2/",
    "SIGNUPS_OPEN": "false",
    "DATABASE_URL": "postgresql://user:pw@ep-prod.neon.tech/rootpulse?sslmode=require",
    "NUM_PROXIES": "1",
    "API_PUBLIC_URL": "https://api.example.com",
    "FRONTEND_URL": "https://app.example.com",
    "CORS_ALLOWED_ORIGINS": "https://app.example.com",
    "AUTH_REFRESH_COOKIE_SAMESITE": "Lax",
    "SCHEDULER_SHARED_SECRET": "a" * 48,
    "PROBER_URLS": "tokyo=https://t.workers.dev,fra=https://f.workers.dev",
    "PROBER_SHARED_SECRET": "b" * 48,
    "EMAIL_HOST": "smtp.resend.com",
    "EMAIL_PORT": "2587",
    "EMAIL_HOST_USER": "resend",
    "EMAIL_HOST_PASSWORD": "re_secret_value",
    "DEFAULT_FROM_EMAIL": "RootPulse <login@example.com>",
    "VAPID_PRIVATE_KEY": "private-key-value",
    "VAPID_PUBLIC_KEY": "public-key-value",
    "VAPID_SUBJECT": "mailto:me@example.com",
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_BOT_USERNAME": "rootpulse_bot",
    "TELEGRAM_WEBHOOK_SECRET": "w" * 32,
    "DISCORD_CLIENT_ID": "d1",
    "DISCORD_CLIENT_SECRET": "d2",
    "SLACK_CLIENT_ID": "s1",
    "SLACK_CLIENT_SECRET": "s2",
}


def levels(env, level):
    return {f.name for f in preflight.check(env) if f.level == level}


def with_(**changes):
    env = dict(GOOD)
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def test_a_correct_production_environment_has_no_failures_or_warnings():
    findings = preflight.check(GOOD)
    assert [f for f in findings if f.level != preflight.OK] == []


@pytest.mark.parametrize(
    ("changes", "name"),
    [
        ({"DJANGO_DEBUG": None}, "DJANGO_DEBUG"),  # unset means debug ON
        ({"DJANGO_DEBUG": "true"}, "DJANGO_DEBUG"),
        ({"DJANGO_SECRET_KEY": None}, "DJANGO_SECRET_KEY"),
        ({"DJANGO_SECRET_KEY": "django-insecure-" + "x" * 60}, "DJANGO_SECRET_KEY"),
        ({"DJANGO_SECRET_KEY": "short"}, "DJANGO_SECRET_KEY"),
        ({"DJANGO_ALLOWED_HOSTS": "*"}, "DJANGO_ALLOWED_HOSTS"),
        ({"DJANGO_ALLOWED_HOSTS": None}, "DJANGO_ALLOWED_HOSTS"),
        ({"DATABASE_URL": None}, "DATABASE_URL"),
        ({"DATABASE_URL": "sqlite:///db.sqlite3"}, "DATABASE_URL"),
        ({"DATABASE_URL": "postgres://u:p@localhost/db"}, "DATABASE_URL"),
        ({"API_PUBLIC_URL": "http://api.example.com"}, "API_PUBLIC_URL"),
        ({"API_PUBLIC_URL": "https://localhost:8000"}, "API_PUBLIC_URL"),
        ({"API_PUBLIC_URL": None}, "API_PUBLIC_URL"),
        ({"FRONTEND_URL": "http://app.example.com"}, "FRONTEND_URL"),
        ({"CORS_ALLOWED_ORIGINS": None}, "CORS_ALLOWED_ORIGINS"),
        ({"CORS_ALLOWED_ORIGINS": "*"}, "CORS_ALLOWED_ORIGINS"),
        ({"CORS_ALLOWED_ORIGINS": "http://app.example.com"}, "CORS_ALLOWED_ORIGINS"),
        ({"CORS_ALLOWED_ORIGINS": "https://elsewhere.example.org"}, "CORS_ALLOWED_ORIGINS"),
        ({"AUTH_REFRESH_COOKIE_SAMESITE": "Bogus"}, "AUTH_REFRESH_COOKIE_SAMESITE"),
        ({"SCHEDULER_SHARED_SECRET": "short"}, "SCHEDULER_SHARED_SECRET"),
        ({"SCHEDULER_SHARED_SECRET": None}, "SCHEDULER_SHARED_SECRET"),
        ({"PROBER_URLS": "tokyo=http://insecure.example"}, "PROBER_URLS"),
        ({"PROBER_SHARED_SECRET": "short"}, "PROBER_SHARED_SECRET"),
        ({"EMAIL_HOST": None}, "EMAIL_HOST"),
        ({"EMAIL_HOST_PASSWORD": None}, "EMAIL_HOST_PASSWORD"),
        ({"VAPID_PUBLIC_KEY": None}, "VAPID_PRIVATE_KEY"),
        ({"TELEGRAM_BOT_USERNAME": None}, "TELEGRAM_BOT_TOKEN"),
        ({"TELEGRAM_WEBHOOK_SECRET": None}, "TELEGRAM_WEBHOOK_SECRET"),
        ({"DISCORD_CLIENT_SECRET": None}, "DISCORD_CLIENT_ID"),
        ({"SLACK_CLIENT_ID": None}, "SLACK_CLIENT_ID"),
        (
            {"GITHUB_OAUTH_CLIENT_ID": "id", "GITHUB_OAUTH_CLIENT_SECRET": "x"},
            "GITHUB_OAUTH_REDIRECT_URI",
        ),
    ],
)
def test_mistakes_fail(changes, name):
    assert name in levels(with_(**changes), preflight.FAIL)


@pytest.mark.parametrize(
    ("changes", "name"),
    [
        ({"DJANGO_ADMIN_URL": "admin/"}, "DJANGO_ADMIN_URL"),
        ({"SIGNUPS_OPEN": None}, "SIGNUPS_OPEN"),  # unset means open
        ({"SIGNUPS_OPEN": "true"}, "SIGNUPS_OPEN"),
        ({"DJANGO_ADMIN_URL": None}, "DJANGO_ADMIN_URL"),
        ({"NUM_PROXIES": "0"}, "NUM_PROXIES"),
        ({"PROBER_URLS": None}, "PROBER_URLS"),
        ({"EMAIL_PORT": "587"}, "EMAIL_PORT"),
        ({"DEFAULT_FROM_EMAIL": None}, "DEFAULT_FROM_EMAIL"),
        ({"VAPID_PRIVATE_KEY": None, "VAPID_PUBLIC_KEY": None}, "VAPID_PRIVATE_KEY"),
        ({"VAPID_SUBJECT": "mailto:alerts@rootpulse.dev"}, "VAPID_SUBJECT"),
        ({"TELEGRAM_BOT_TOKEN": None, "TELEGRAM_BOT_USERNAME": None}, "TELEGRAM_BOT_TOKEN"),
        ({"DISCORD_CLIENT_ID": None, "DISCORD_CLIENT_SECRET": None}, "DISCORD_CLIENT_ID"),
        ({"SLACK_CLIENT_ID": None, "SLACK_CLIENT_SECRET": None}, "SLACK_CLIENT_ID"),
        ({"FRONTEND_URL": "https://app.example.com/"}, "FRONTEND_URL"),
        ({"AUTH_REFRESH_COOKIE_SAMESITE": "None"}, "AUTH_REFRESH_COOKIE_SAMESITE"),
    ],
)
def test_risks_warn_without_failing(changes, name):
    env = with_(**changes)
    assert name in levels(env, preflight.WARN)
    assert preflight.FAIL not in {f.level for f in preflight.check(env) if f.name == name}


def test_the_cookie_rule_follows_whether_the_two_addresses_are_one_site():
    # Different sites: only SameSite=None is deliverable, and even that is a warning.
    cross = {
        "API_PUBLIC_URL": "https://rootpulse-api.onrender.com",
        "FRONTEND_URL": "https://rootpulse.vercel.app",
        "CORS_ALLOWED_ORIGINS": "https://rootpulse.vercel.app",
    }
    assert "AUTH_REFRESH_COOKIE_SAMESITE" in levels(
        with_(**cross, AUTH_REFRESH_COOKIE_SAMESITE="Lax"), preflight.FAIL
    )
    assert "AUTH_REFRESH_COOKIE_SAMESITE" in levels(
        with_(**cross, AUTH_REFRESH_COOKIE_SAMESITE="None"), preflight.WARN
    )
    # Siblings under one domain: Lax is right and None is needlessly loose.
    assert "AUTH_REFRESH_COOKIE_SAMESITE" not in levels(with_(), preflight.WARN) | levels(
        with_(), preflight.FAIL
    )


def test_a_local_default_environment_is_rejected_outright():
    findings = preflight.check({})
    assert sum(f.level == preflight.FAIL for f in findings) >= 8


def test_the_hosting_platforms_own_hostname_satisfies_allowed_hosts():
    env = with_(DJANGO_ALLOWED_HOSTS=None, RENDER_EXTERNAL_HOSTNAME="rootpulse-api.onrender.com")
    assert "DJANGO_ALLOWED_HOSTS" not in levels(env, preflight.FAIL)


def test_the_report_never_contains_a_value(tmp_path, capsys):
    env_file = tmp_path / "prod.env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in GOOD.items()), encoding="utf-8")
    assert preflight.main(["--env-file", str(env_file)]) == 0
    printed = capsys.readouterr().out
    for name, value in GOOD.items():
        if len(value) >= 8:
            assert value not in printed, f"{name}'s value was printed"
    assert "0 failed" in printed


def test_exit_status_reflects_failures_and_strictness(tmp_path, capsys):
    bad = tmp_path / "bad.env"
    bad.write_text("DJANGO_DEBUG=true\n", encoding="utf-8")
    assert preflight.main(["--env-file", str(bad)]) == 1

    warny = tmp_path / "warny.env"
    warny.write_text(
        "\n".join(f"{k}={v}" for k, v in with_(NUM_PROXIES="0").items()), encoding="utf-8"
    )
    assert preflight.main(["--env-file", str(warny)]) == 0
    assert preflight.main(["--env-file", str(warny), "--strict"]) == 1
    capsys.readouterr()


PROXIED = {
    "API_PUBLIC_URL": "https://rootpulse-api.onrender.com",
    "FRONTEND_URL": "https://rootpulse.vercel.app",
    "CORS_ALLOWED_ORIGINS": "https://rootpulse.vercel.app",
    "AUTH_REFRESH_COOKIE_SAMESITE": "Lax",
    "NUM_PROXIES": "2",
}


def test_behind_the_websites_proxy_lax_on_two_sites_is_right_and_two_proxies_are_expected():
    findings = preflight.check(with_(**PROXIED), proxied=True)
    assert [f for f in findings if f.level != preflight.OK] == []
    # The same settings without saying so are a mistake: the cookie would never be sent.
    assert "AUTH_REFRESH_COOKIE_SAMESITE" in levels(with_(**PROXIED), preflight.FAIL)


def test_the_proxy_needs_two_proxy_hops_and_a_callback_on_the_website():
    assert "NUM_PROXIES" in {
        f.name
        for f in preflight.check(with_(**{**PROXIED, "NUM_PROXIES": "1"}), proxied=True)
        if f.level == preflight.WARN
    }
    env = with_(
        **PROXIED,
        GITHUB_OAUTH_CLIENT_ID="id",
        GITHUB_OAUTH_CLIENT_SECRET="secret",
        GITHUB_OAUTH_REDIRECT_URI="https://rootpulse-api.onrender.com/api/v1/auth/github/callback/",
    )
    failed = {f.name for f in preflight.check(env, proxied=True) if f.level == preflight.FAIL}
    assert "GITHUB_OAUTH_REDIRECT_URI" in failed
    env["GITHUB_OAUTH_REDIRECT_URI"] = "https://rootpulse.vercel.app/api/v1/auth/github/callback/"
    assert preflight.FAIL not in {f.level for f in preflight.check(env, proxied=True)}


def test_none_behind_the_proxy_is_a_needless_loosening():
    env = with_(**{**PROXIED, "AUTH_REFRESH_COOKIE_SAMESITE": "None"})
    warned = {f.name for f in preflight.check(env, proxied=True) if f.level == preflight.WARN}
    assert "AUTH_REFRESH_COOKIE_SAMESITE" in warned


def test_the_proxied_flag_is_passed_through_the_command_line(tmp_path, capsys):
    env_file = tmp_path / "proxied.env"
    lines = [f"{k}={v}" for k, v in with_(**PROXIED).items()]
    env_file.write_text(chr(10).join(lines), encoding="utf-8")
    assert preflight.main(["--env-file", str(env_file), "--proxied", "--strict"]) == 0
    assert preflight.main(["--env-file", str(env_file)]) == 1
    capsys.readouterr()
