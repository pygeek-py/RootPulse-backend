"""Turning each provider's webhook into the same thing: a verified, parsed deploy.

For every source, `verify_and_parse` first checks the provider's own signature over the raw body
(nothing in the body is looked at before that), then reads what it needs. A request that is
genuine but isn't a successful deploy (a ping, a failed run, a preview) raises `Ignored`, which
is answered with a polite 202 rather than an error.

Payload fields are read defensively, because a deploy event is an outside document: a missing
field falls back to something sensible and never raises.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from monitoring import signing

from .models import DeploySource

TOLERANCE_SECONDS = 300  # how stale a timestamped signature may be (Render, generic)


class Rejected(Exception):
    """The request can't be trusted (bad or missing signature, malformed body)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Ignored(Exception):
    """A genuine request that isn't a successful deploy. `reason` is safe to show."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ParsedDeploy:
    external_id: str
    service_name: str
    aliases: list[str] = field(default_factory=list)
    environment: str = ""
    version: str = ""
    url: str = ""
    occurred_at: datetime | None = None
    #: The part of the payload worth keeping (never the whole thing: it is outside data).
    summary: dict[str, Any] = field(default_factory=dict)


# --- small cleaning helpers ------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean(value: Any, limit: int) -> str:
    """A short, printable string; anything that isn't text becomes empty."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return _CONTROL.sub("", str(value)).strip()[:limit]


def clean_url(value: Any) -> str:
    """Only plain http(s) links are kept: this ends up in an <a href> in the UI."""
    text = clean(value, 500)
    return text if re.match(r"^https?://[^\s]+$", text, re.IGNORECASE) else ""


def short_ref(value: Any) -> str:
    text = clean(value, 80)
    return text[:7] if re.fullmatch(r"[0-9a-f]{40}", text) else text


def parse_time(value: Any) -> datetime | None:
    """ISO text, or epoch seconds/milliseconds."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=UTC)
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Rejected(400, "The body isn't valid JSON.") from exc
    if not isinstance(data, dict):
        raise Rejected(400, "The body must be a JSON object.")
    return data


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- generic: RootPulse's own signing scheme -------------------------------------------


def _generic(source: DeploySource, headers, body: bytes) -> ParsedDeploy:
    if not signing.verify(
        source.secret, headers.get(signing.HEADER), body, tolerance=TOLERANCE_SECONDS
    ):
        raise Rejected(401, "Bad or missing signature.")
    data = _json(body)
    service = clean(data.get("service"), 150)
    if not service:
        raise Rejected(400, 'Say which service this deploy is for, e.g. {"service": "my-api"}.')
    version = short_ref(data.get("version"))
    external = clean(data.get("id"), 128) or hashlib.sha256(body).hexdigest()[:32]
    return ParsedDeploy(
        external_id=external,
        service_name=service,
        environment=clean(data.get("environment"), 60),
        version=version,
        url=clean_url(data.get("url")),
        occurred_at=parse_time(data.get("occurred_at")),
        summary={"service": service, "version": version},
    )


# --- GitHub: X-Hub-Signature-256 -------------------------------------------------------


def _github(source: DeploySource, headers, body: bytes) -> ParsedDeploy:
    sent = headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(source.secret.encode(), body, hashlib.sha256).hexdigest()
    if not sent or not _equal(sent, expected):
        raise Rejected(401, "Bad or missing signature.")
    event = headers.get("X-GitHub-Event", "")
    if event == "ping":
        raise Ignored("ping")
    data = _json(body)
    repo = data.get("repository") if isinstance(data.get("repository"), dict) else {}
    name, full = clean(repo.get("name"), 150), clean(repo.get("full_name"), 150)
    if not name:
        raise Ignored("no repository in the event")

    if event == "deployment_status":
        status = _dig(data, "deployment_status", "state")
        if status != "success":
            raise Ignored(f"deployment {clean(status, 20) or 'status'}")
        deployment = data.get("deployment") if isinstance(data.get("deployment"), dict) else {}
        return ParsedDeploy(
            external_id=f"deployment:{clean(deployment.get('id'), 40)}",
            service_name=name,
            aliases=[full] if full else [],
            environment=clean(deployment.get("environment"), 60),
            version=short_ref(deployment.get("sha") or deployment.get("ref")),
            url=clean_url(
                _dig(data, "deployment_status", "environment_url")
                or _dig(data, "deployment_status", "target_url")
            ),
            occurred_at=parse_time(_dig(data, "deployment_status", "created_at")),
            summary={"event": event, "repository": full or name},
        )

    if event == "workflow_run":
        run = data.get("workflow_run") if isinstance(data.get("workflow_run"), dict) else {}
        if data.get("action") != "completed" or run.get("conclusion") != "success":
            raise Ignored("workflow run not completed successfully")
        workflow = clean(run.get("name"), 120)
        # A CI run that merely tests isn't a deploy. A workflow counts when it is named like one.
        if not re.search(r"deploy|release|ship|publish", workflow, re.IGNORECASE):
            raise Ignored("workflow isn't named like a deploy")
        return ParsedDeploy(
            external_id=f"run:{clean(run.get('id'), 40)}:{clean(run.get('run_attempt'), 4) or '1'}",
            service_name=name,
            aliases=[full] if full else [],
            environment="",
            version=short_ref(run.get("head_sha")),
            url=clean_url(run.get("html_url")),
            occurred_at=parse_time(run.get("updated_at") or run.get("run_started_at")),
            summary={"event": event, "workflow": workflow, "repository": full or name},
        )

    raise Ignored(f"event '{clean(event, 40) or 'unknown'}' isn't a deploy")


# --- Vercel: x-vercel-signature (HMAC-SHA1 of the body) --------------------------------


def _vercel(source: DeploySource, headers, body: bytes) -> ParsedDeploy:
    sent = headers.get("x-vercel-signature", "")
    expected = hmac.new(
        source.secret.encode(), body, hashlib.sha1
    ).hexdigest()  # noqa: S324 - Vercel's scheme
    if not sent or not _equal(sent, expected):
        raise Rejected(401, "Bad or missing signature.")
    data = _json(body)
    kind = clean(data.get("type"), 60)
    if kind not in ("deployment.succeeded", "deployment.ready"):
        raise Ignored(f"event '{kind or 'unknown'}' isn't a finished deploy")
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    deployment = payload.get("deployment") if isinstance(payload.get("deployment"), dict) else {}
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    service = clean(payload.get("name") or deployment.get("name"), 150)
    if not service:
        raise Ignored("no project name in the event")
    host = clean(deployment.get("url"), 300)
    link = clean_url(f"https://{host}") if host and "://" not in host else clean_url(host)
    meta = deployment.get("meta") if isinstance(deployment.get("meta"), dict) else {}
    return ParsedDeploy(
        external_id=f"deployment:{clean(deployment.get('id'), 60)}",
        service_name=service,
        aliases=[a for a in [clean(project.get("id"), 80)] if a],
        environment=clean(payload.get("target"), 60) or "production",
        version=short_ref(meta.get("githubCommitSha") or deployment.get("id")),
        url=link,
        occurred_at=parse_time(data.get("createdAt")),
        summary={"event": kind, "project": service},
    )


# --- Render: Svix-style signature (webhook-id, -timestamp, -signature) -----------------


def _svix_key(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        try:
            return base64.b64decode(secret[len("whsec_") :])
        except ValueError:
            pass
    return secret.encode()


def _render(source: DeploySource, headers, body: bytes) -> ParsedDeploy:
    message_id = headers.get("webhook-id", "")
    stamp = headers.get("webhook-timestamp", "")
    sent = headers.get("webhook-signature", "")
    try:
        age = abs(datetime.now(UTC).timestamp() - int(stamp))
    except ValueError:
        raise Rejected(401, "Bad or missing signature.") from None
    if not message_id or not sent or age > TOLERANCE_SECONDS:
        raise Rejected(401, "Bad or missing signature.")
    signed = f"{message_id}.{stamp}.".encode() + body
    expected = base64.b64encode(hmac.new(_svix_key(source.secret), signed, hashlib.sha256).digest())
    # The header can carry several space-separated "v1,<signature>" entries (key rotation).
    candidates = [part.split(",", 1)[1] for part in sent.split() if part.startswith("v1,")]
    if not any(_equal(candidate, expected.decode()) for candidate in candidates):
        raise Rejected(401, "Bad or missing signature.")
    data = _json(body)
    kind = clean(data.get("type"), 60)
    info = data.get("data") if isinstance(data.get("data"), dict) else {}
    if kind != "deploy_ended":
        raise Ignored(f"event '{kind or 'unknown'}' isn't a finished deploy")
    if clean(info.get("status"), 30) not in ("succeeded", "live", "success"):
        raise Ignored(f"deploy {clean(info.get('status'), 30) or 'did not succeed'}")
    service_id = clean(info.get("serviceId"), 80)
    name = clean(info.get("serviceName"), 150) or service_id
    if not name:
        raise Ignored("no service in the event")
    return ParsedDeploy(
        external_id=f"deploy:{clean(info.get('id'), 80) or message_id}",
        service_name=name,
        aliases=[service_id] if service_id and service_id != name else [],
        environment="",
        version=short_ref(info.get("commitId") or info.get("id")),
        occurred_at=parse_time(data.get("timestamp")),
        summary={"event": kind, "service": name},
    )


PARSERS = {
    DeploySource.Type.GENERIC: _generic,
    DeploySource.Type.GITHUB: _github,
    DeploySource.Type.VERCEL: _vercel,
    DeploySource.Type.RENDER: _render,
}


def verify_and_parse(source: DeploySource, headers, body: bytes) -> ParsedDeploy:
    # A connection still waiting for its secret trusts nobody. (Without this, a signature made
    # with an empty key would verify.)
    if not source.secret:
        raise Rejected(401, "Bad or missing signature.")
    return PARSERS[source.type](source, headers, body)
