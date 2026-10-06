"""Check a production environment for mistakes BEFORE deploying it.

    python scripts/preflight.py                      # the current environment (and ./.env)
    python scripts/preflight.py --env-file prod.env  # a file holding the production values
    python scripts/preflight.py --strict             # warnings fail too
    python scripts/preflight.py --proxied            # the website proxies the API

It only reads the values and judges their shape (is it https, is it long enough, do the two
halves of a pair both exist). It never prints a value, only the name of the setting and what is
wrong with it, so its output is safe to paste anywhere. It makes no network calls.

Exit status: 0 when nothing failed (and, with --strict, nothing warned), 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

FAIL, WARN, OK = "FAIL", "WARN", "ok"

# Hosts (and name endings) that can never be a real, public deployment.
LOCAL_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
}  # noqa: S104 - a list to refuse, not to bind
DEFAULT_FROM = "RootPulse <noreply@rootpulse.dev>"
DEFAULT_VAPID_SUBJECT = "mailto:alerts@rootpulse.dev"


@dataclass(frozen=True)
class Finding:
    level: str
    name: str
    message: str


def _truthy(value: str | None, default: bool) -> bool:
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _is_local(host: str) -> bool:
    return host in LOCAL_HOSTS or host.endswith(".localhost") or host.endswith(".test")


def _registrable(host: str) -> str:
    """A rough 'site' for a host: its last two labels (good enough to spot siblings; it does
    not know the public suffix list, so *.vercel.app and *.onrender.com are special-cased)."""
    for shared in ("vercel.app", "onrender.com", "pages.dev", "workers.dev", "netlify.app"):
        if host == shared or host.endswith("." + shared):
            return host  # every one of these is its own site
    labels = host.split(".")
    return ".".join(labels[-2:])


def check(env: Mapping[str, str], *, proxied: bool = False) -> list[Finding]:
    """`proxied`: the website forwards /api/v1/* to the API (the browser only ever talks to the
    website), so the cookie is first-party and one more proxy sits in front of the API."""
    out: list[Finding] = []

    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    def fail(name: str, message: str) -> None:
        out.append(Finding(FAIL, name, message))

    def warn(name: str, message: str) -> None:
        out.append(Finding(WARN, name, message))

    def good(name: str, message: str) -> None:
        out.append(Finding(OK, name, message))

    def need_https_url(name: str, *, public: bool = True) -> str:
        value = get(name)
        if not value:
            fail(name, "is not set")
            return ""
        parts = urlsplit(value)
        if parts.scheme != "https":
            fail(name, "must start with https://")
        elif public and _is_local(_host(value)):
            fail(name, "points at a local address")
        elif value.endswith("/"):
            warn(name, "has a trailing slash; remove it")
        elif parts.path not in ("", "/"):
            warn(name, "has a path; it should be just the address")
        else:
            good(name, "is an https address")
        return value

    # --- Core -------------------------------------------------------------------------------
    if _truthy(env.get("DJANGO_DEBUG"), True):
        fail("DJANGO_DEBUG", "must be false in production (it defaults to true when unset)")
    else:
        good("DJANGO_DEBUG", "is off")

    secret = get("DJANGO_SECRET_KEY")
    if not secret:
        fail("DJANGO_SECRET_KEY", "is not set")
    elif secret.startswith("django-insecure") or "change-me" in secret:
        fail("DJANGO_SECRET_KEY", "is a placeholder; generate a random one")
    elif len(secret) < 50:
        fail("DJANGO_SECRET_KEY", "is shorter than 50 characters")
    else:
        good("DJANGO_SECRET_KEY", "is long enough")

    hosts = _split(get("DJANGO_ALLOWED_HOSTS"))
    if not hosts and not get("RENDER_EXTERNAL_HOSTNAME"):
        fail("DJANGO_ALLOWED_HOSTS", "is not set")
    elif "*" in hosts:
        fail("DJANGO_ALLOWED_HOSTS", "contains *, which accepts any Host header")
    elif any(_is_local(h) for h in hosts):
        warn("DJANGO_ALLOWED_HOSTS", "still lists a local address")
    else:
        good("DJANGO_ALLOWED_HOSTS", "lists only real hosts")

    admin = get("DJANGO_ADMIN_URL").strip("/")
    if not admin or admin == "admin":
        warn("DJANGO_ADMIN_URL", "is the well-known 'admin/'; choose something unguessable")
    else:
        good("DJANGO_ADMIN_URL", "is not the default")

    db = get("DATABASE_URL")
    if not db:
        fail("DATABASE_URL", "is not set (the app would fall back to a local database)")
    elif not db.startswith(("postgres://", "postgresql://")):
        fail("DATABASE_URL", "must be a postgres:// or postgresql:// URL")
    elif _is_local(_host(db)):
        fail("DATABASE_URL", "points at a local database")
    else:
        good("DATABASE_URL", "is a remote Postgres URL")

    if _truthy(env.get("SIGNUPS_OPEN"), True):
        warn(
            "SIGNUPS_OPEN",
            "is on: anyone who finds the site can create an account and use your free-tier "
            "database. Create your own account, then set it to false",
        )
    else:
        good("SIGNUPS_OPEN", "new accounts are closed")

    expected_proxies = "2" if proxied else "1"
    if get("NUM_PROXIES") != expected_proxies:
        warn(
            "NUM_PROXIES",
            f"should be {expected_proxies} ("
            + ("the website's proxy, then Render's" if proxied else "Render's load balancer")
            + "), or every visitor looks like one address to the rate limits (too low) or can "
            "fake theirs (too high)",
        )
    else:
        good("NUM_PROXIES", f"is {expected_proxies}")

    # --- Addresses and cookies --------------------------------------------------------------
    api = need_https_url("API_PUBLIC_URL")
    site = need_https_url("FRONTEND_URL")

    cors = _split(get("CORS_ALLOWED_ORIGINS"))
    if not cors:
        fail("CORS_ALLOWED_ORIGINS", "is not set; the website could not call the API")
    elif "*" in cors:
        fail("CORS_ALLOWED_ORIGINS", "contains *")
    elif any(not c.startswith("https://") for c in cors):
        fail("CORS_ALLOWED_ORIGINS", "lists an origin that is not https")
    elif site and _origin(site) not in [c.rstrip("/").lower() for c in cors]:
        fail("CORS_ALLOWED_ORIGINS", "does not include the FRONTEND_URL origin")
    else:
        good("CORS_ALLOWED_ORIGINS", "includes the website")

    samesite = get("AUTH_REFRESH_COOKIE_SAMESITE") or "None"
    if samesite not in ("None", "Lax", "Strict"):
        fail("AUTH_REFRESH_COOKIE_SAMESITE", "must be None, Lax or Strict")
    elif proxied:
        if samesite == "None":
            warn(
                "AUTH_REFRESH_COOKIE_SAMESITE",
                "is None, but through the website's proxy the cookie is first-party: use Lax",
            )
        else:
            good("AUTH_REFRESH_COOKIE_SAMESITE", f"is {samesite}, right behind the proxy")
        callback = get("GITHUB_OAUTH_REDIRECT_URI")
        if get("GITHUB_OAUTH_CLIENT_ID") and site and _host(callback) != _host(site):
            fail(
                "GITHUB_OAUTH_REDIRECT_URI",
                "must be on the website's address when the website proxies the API, so the "
                "sign-in cookie is set on the website",
            )
    elif api and site:
        same_site = _registrable(_host(api)) == _registrable(_host(site))
        if samesite == "None" and same_site:
            warn(
                "AUTH_REFRESH_COOKIE_SAMESITE",
                "is None though the website and API share a site; Lax is safer here",
            )
        elif samesite != "None" and not same_site:
            fail(
                "AUTH_REFRESH_COOKIE_SAMESITE",
                f"is {samesite} but the website and API are different sites, so the browser "
                "would never send the sign-in cookie. Use None, put both under one domain, "
                "or proxy the API through the website and pass --proxied "
                "(docs/plan/09-deployment-runbook.md)",
            )
        elif samesite == "None":
            warn(
                "AUTH_REFRESH_COOKIE_SAMESITE",
                "is None because the website and API are different sites; Safari and some "
                "browsers block that cookie. A shared domain or the proxy is more reliable",
            )
        else:
            good("AUTH_REFRESH_COOKIE_SAMESITE", f"is {samesite}, right for one site")

    # --- Scheduler and probers --------------------------------------------------------------
    sched = get("SCHEDULER_SHARED_SECRET")
    if len(sched) < 32:
        fail("SCHEDULER_SHARED_SECRET", "is missing or shorter than 32 characters")
    else:
        good("SCHEDULER_SHARED_SECRET", "is long enough")

    probers = get("PROBER_URLS")
    if probers:
        bad = []
        for entry in _split(probers):
            name, _, url = entry.partition("=")
            if not name or not url.startswith("https://"):
                bad.append(name or "?")
        if bad:
            fail(
                "PROBER_URLS", "has an entry that is not name=https://...; check: " + ", ".join(bad)
            )
        elif len(get("PROBER_SHARED_SECRET")) < 32:
            fail("PROBER_SHARED_SECRET", "is missing or shorter than 32 characters")
        else:
            good("PROBER_URLS", f"{len(_split(probers))} prober(s), with a secret")
    else:
        warn(
            "PROBER_URLS",
            "is empty: failures will only be re-checked from the API's own region, not from "
            "other regions",
        )

    # --- Email ------------------------------------------------------------------------------
    email_host = get("EMAIL_HOST")
    if not email_host:
        fail("EMAIL_HOST", "is not set: sign-in codes would only print to the log")
    else:
        port = get("EMAIL_PORT") or "587"
        if port == "587" and not _truthy(env.get("EMAIL_USE_SSL"), False):
            warn(
                "EMAIL_PORT",
                "587 is blocked on Render's free tier; Resend also listens on 2587 or 2465",
            )
        if not get("EMAIL_HOST_USER") or not get("EMAIL_HOST_PASSWORD"):
            fail("EMAIL_HOST_PASSWORD", "or EMAIL_HOST_USER is missing")
        else:
            good("EMAIL_HOST", "has credentials")
    if get("DEFAULT_FROM_EMAIL") in ("", DEFAULT_FROM):
        warn(
            "DEFAULT_FROM_EMAIL",
            "is the placeholder; mail must come from an address on a domain you've verified "
            "with your provider or it is rejected or lands in spam",
        )

    # --- Web push ---------------------------------------------------------------------------
    vapid = [get("VAPID_PRIVATE_KEY"), get("VAPID_PUBLIC_KEY")]
    if any(vapid) and not all(vapid):
        fail("VAPID_PRIVATE_KEY", "and VAPID_PUBLIC_KEY must both be set, or both empty")
    elif not any(vapid):
        warn("VAPID_PRIVATE_KEY", "is empty: browser push notifications are off")
    elif get("VAPID_SUBJECT") in ("", DEFAULT_VAPID_SUBJECT):
        warn("VAPID_SUBJECT", "is the placeholder; use mailto: your own address")
    else:
        good("VAPID_PRIVATE_KEY", "push keys are set")

    # --- One-click alert channels (the pre-deploy reminder) ---------------------------------
    tg = [get("TELEGRAM_BOT_TOKEN"), get("TELEGRAM_BOT_USERNAME")]
    if any(tg) and not all(tg):
        fail("TELEGRAM_BOT_TOKEN", "and TELEGRAM_BOT_USERNAME must both be set")
    elif all(tg):
        if len(get("TELEGRAM_WEBHOOK_SECRET")) < 16:
            fail(
                "TELEGRAM_WEBHOOK_SECRET",
                "is missing or too short; without it Telegram can't reach the API in production",
            )
        else:
            good(
                "TELEGRAM_BOT_TOKEN",
                "set, with a webhook secret (now run: manage.py telegram_webhook set)",
            )
    else:
        warn("TELEGRAM_BOT_TOKEN", "is empty: 'Connect Telegram' is off")

    for label, a, b in (
        ("Discord", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET"),
        ("Slack", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"),
    ):
        pair = [get(a), get(b)]
        if any(pair) and not all(pair):
            fail(a, f"and {b} must both be set")
        elif not any(pair):
            warn(a, f"is empty: 'Connect {label}' is off")
        else:
            good(a, f"{label} app credentials are set")

    github = [get("GITHUB_OAUTH_CLIENT_ID"), get("GITHUB_OAUTH_CLIENT_SECRET")]
    if any(github):
        redirect = get("GITHUB_OAUTH_REDIRECT_URI")
        if not all(github):
            fail("GITHUB_OAUTH_CLIENT_ID", "and GITHUB_OAUTH_CLIENT_SECRET must both be set")
        elif not redirect.startswith("https://") or _is_local(_host(redirect)):
            fail("GITHUB_OAUTH_REDIRECT_URI", "must be the https callback on the production host")

    return out


def load_env_file(path: str) -> dict[str, str]:
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", help="a dotenv file holding the production values")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument(
        "--proxied",
        action="store_true",
        help="the website forwards /api/v1/* to the API (NEXT_PUBLIC_API_URL left empty)",
    )
    args = parser.parse_args(argv)

    if args.env_file:
        env = load_env_file(args.env_file)
        source = args.env_file
    else:
        env = dict(os.environ)
        if os.path.exists(".env"):
            for key, value in load_env_file(".env").items():
                env.setdefault(key, value)
        source = "the current environment"

    findings = check(env, proxied=args.proxied)
    print(f"Preflight for {source}\n")
    for level in (FAIL, WARN, OK):
        for f in (x for x in findings if x.level == level):
            print(f"  {f.level:<4}  {f.name}: {f.message}")
    fails = sum(f.level == FAIL for f in findings)
    warns = sum(f.level == WARN for f in findings)
    print(f"\n{fails} failed, {warns} warnings, {len(findings) - fails - warns} ok")
    return 1 if fails or (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
