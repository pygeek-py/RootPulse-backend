"""Minimal GitHub OAuth2 (authorization-code) client — a few `httpx` calls
instead of django-allauth, which assumes a server-rendered app
(docs/plan/01-tech-stack.md)."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from django.conf import settings

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_URL = "https://api.github.com"
TIMEOUT = httpx.Timeout(10.0)


class GitHubError(Exception):
    """Anything that goes wrong talking to GitHub; the view turns it into a
    friendly redirect rather than leaking the details."""


@dataclass(frozen=True)
class GitHubProfile:
    github_id: str
    email: str  # primary *verified* email — never an unverified one


def is_configured() -> bool:
    return bool(settings.GITHUB_OAUTH_CLIENT_ID and settings.GITHUB_OAUTH_CLIENT_SECRET)


def authorize_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
            "scope": "read:user user:email",
            "state": state,
            "allow_signup": "true",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def fetch_profile(code: str) -> GitHubProfile:
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            token_resp = client.post(
                TOKEN_URL,
                data={
                    "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
                    "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            access_token = token_resp.json().get("access_token")
            if not access_token:
                raise GitHubError("GitHub did not return an access token.")

            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            }
            user_resp = client.get(f"{API_URL}/user", headers=headers)
            user_resp.raise_for_status()
            emails_resp = client.get(f"{API_URL}/user/emails", headers=headers)
            emails_resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise GitHubError("GitHub request failed.") from exc

    github_id = user_resp.json().get("id")
    verified = [e for e in emails_resp.json() if e.get("verified") and e.get("email")]
    primary = next((e for e in verified if e.get("primary")), verified[0] if verified else None)
    if not github_id or not primary:
        raise GitHubError("GitHub account has no verified email.")

    return GitHubProfile(github_id=str(github_id), email=primary["email"].strip().lower())
