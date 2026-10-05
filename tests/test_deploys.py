"""Deploy webhooks (per-provider signatures), linking deploys to incidents, root-cause stage,
and how all of it shows up in the API, alerts and postmortem."""

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from django.urls import Resolver404, resolve
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AuditLog
from analytics.models import CheckRollupDaily
from deploys import parsers, rootcause
from deploys import services as deploy_services
from deploys.models import Deploy, DeploySource
from incidents import services as incident_services
from incidents.models import Incident, IncidentEvent
from monitoring import signing
from monitoring.models import AlertContact, Check, Monitor
from notifications import dispatcher, messages
from notifications.models import NotificationDelivery
from tests.conftest import ORIGIN

pytestmark = pytest.mark.django_db

SECRET = "s3cret-signing-key"


def make_source(user, type="generic", **kwargs):
    fields = {"name": f"my {type}", "type": type, "secret": SECRET}
    fields.update(kwargs)
    return DeploySource.objects.create(user=user, **fields)


def url(source, type=None):
    return f"/api/v1/deploys/webhook/{type or source.type}/{source.token}/"


def post(client, source, body, headers=None, type=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return client.generic(
        "POST", url(source, type), raw, content_type="application/json", **(headers or {})
    )


def generic_headers(body, secret=SECRET, now=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return {"HTTP_X_ROOTPULSE_SIGNATURE": signing.sign(secret, raw, now=now)}


def github_headers(body, event, secret=SECRET):
    raw = json.dumps(body).encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {"HTTP_X_HUB_SIGNATURE_256": f"sha256={digest}", "HTTP_X_GITHUB_EVENT": event}


def vercel_headers(body, secret=SECRET):
    raw = json.dumps(body).encode()
    return {"HTTP_X_VERCEL_SIGNATURE": hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest()}


def render_headers(body, secret=SECRET, message_id="msg_1", stamp=None, extra_signatures=()):
    raw = json.dumps(body).encode()
    stamp = str(int(time.time()) if stamp is None else stamp)
    key = base64.b64decode(secret[6:]) if secret.startswith("whsec_") else secret.encode()
    sig = base64.b64encode(
        hmac.new(key, f"{message_id}.{stamp}.".encode() + raw, hashlib.sha256).digest()
    ).decode()
    signature = " ".join([*(f"v1,{s}" for s in extra_signatures), f"v1,{sig}"])
    return {
        "HTTP_WEBHOOK_ID": message_id,
        "HTTP_WEBHOOK_TIMESTAMP": stamp,
        "HTTP_WEBHOOK_SIGNATURE": signature,
    }


def make_monitor(user, name="Shop", tag="my-api", **kwargs):
    fields = {
        "type": "http",
        "target": "https://shop.example.com",
        "interval_seconds": 300,
        "config": {},
        "status": "up",
        "next_check_at": timezone.now(),
        "deploy_service": tag,
    }
    fields.update(kwargs)
    return Monitor.objects.create(user=user, name=name, **fields)


def make_deploy(user, service="my-api", minutes_ago=3, names=None, **kwargs):
    return Deploy.objects.create(
        user=user,
        source_type="generic",
        source_name="CI",
        external_id=f"id-{Deploy.objects.count()}",
        service_name=service,
        names=names or [service.lower()],
        occurred_at=timezone.now() - timedelta(minutes=minutes_ago),
        **kwargs,
    )


def failing_check(monitor, minutes_ago=0, **kwargs):
    fields = {
        "monitor": monitor,
        "region": "primary",
        "started_at": timezone.now() - timedelta(minutes=minutes_ago),
        "success": False,
        "status_detail": "status_mismatch",
        "status_code": 503,
        "duration_ms": 90,
    }
    fields.update(kwargs)
    return Check.objects.create(**fields)


def open_incident(monitor, **check_kwargs):
    check = failing_check(monitor, **check_kwargs)
    incident = incident_services.open_incident(monitor, check)
    assert incident is not None
    return incident


GITHUB_DEPLOY = {
    "action": "created",
    "deployment_status": {
        "state": "success",
        "created_at": "2026-10-04T12:00:00Z",
        "environment_url": "https://app.example.com",
    },
    "deployment": {"id": 4821, "sha": "a" * 40, "environment": "production", "ref": "main"},
    "repository": {"name": "my-api", "full_name": "acme/my-api"},
}


# --- the generic webhook ---------------------------------------------------------------


class TestGenericWebhook:
    BODY = {"service": "my-api", "version": "v1.4.2", "environment": "production"}

    def test_a_signed_deploy_is_recorded(self, api, user):
        source = make_source(user)
        resp = post(api, source, self.BODY, generic_headers(self.BODY))
        assert resp.status_code == 200 and resp.json()["status"] == "recorded"
        deploy = Deploy.objects.get()
        assert (deploy.service_name, deploy.version, deploy.environment) == (
            "my-api",
            "v1.4.2",
            "production",
        )
        assert (deploy.user, deploy.source, deploy.names) == (user, source, ["my-api"])
        source.refresh_from_db()
        assert source.last_received_at is not None

    @pytest.mark.parametrize("headers", [{}, {"HTTP_X_ROOTPULSE_SIGNATURE": "t=1,v1=nope"}])
    def test_missing_or_bad_signatures_are_refused(self, api, user, headers):
        resp = post(api, make_source(user), self.BODY, headers)
        assert resp.status_code == 401 and not Deploy.objects.exists()

    def test_a_signature_from_another_secret_is_refused(self, api, user):
        resp = post(api, make_source(user), self.BODY, generic_headers(self.BODY, secret="other"))
        assert resp.status_code == 401

    def test_an_old_signature_cannot_be_replayed(self, api, user):
        stale = generic_headers(self.BODY, now=time.time() - 3600)
        assert post(api, make_source(user), self.BODY, stale).status_code == 401

    def test_a_signature_does_not_cover_a_different_body(self, api, user):
        headers = generic_headers(self.BODY)
        tampered = {**self.BODY, "service": "other"}
        assert post(api, make_source(user), tampered, headers).status_code == 401

    def test_the_same_deploy_twice_is_recorded_once(self, api, user):
        source = make_source(user)
        body = {**self.BODY, "id": "build-77"}
        first = post(api, source, body, generic_headers(body))
        second = post(api, source, body, generic_headers(body))
        assert second.json()["status"] == "duplicate"
        assert second.json()["deploy_id"] == first.json()["deploy_id"]
        assert Deploy.objects.count() == 1

    def test_without_an_id_an_identical_body_is_still_deduplicated(self, api, user):
        source = make_source(user)
        for _ in range(2):
            post(api, source, self.BODY, generic_headers(self.BODY))
        assert Deploy.objects.count() == 1

    def test_the_service_is_required(self, api, user):
        body = {"version": "1"}
        resp = post(api, make_source(user), body, generic_headers(body))
        assert resp.status_code == 400 and "service" in resp.json()["detail"]

    def test_garbage_that_is_correctly_signed_is_a_400(self, api, user):
        for raw in (b"not json", b"[1, 2]"):
            resp = post(api, make_source(user), raw, generic_headers(raw))
            assert resp.status_code == 400

    def test_oversized_bodies_are_refused(self, api, user, settings):
        settings.DEPLOY_MAX_BODY_BYTES = 200
        body = {**self.BODY, "padding": "x" * 500}
        assert post(api, make_source(user), body, generic_headers(body)).status_code == 413

    def test_the_providers_clock_is_trusted_only_when_plausible(self, api, user):
        source = make_source(user)
        recent = (timezone.now() - timedelta(minutes=10)).isoformat()
        for n, when in enumerate((recent, "2099-01-01T00:00:00Z", "2001-01-01T00:00:00Z", "junk")):
            body = {**self.BODY, "id": f"d{n}", "occurred_at": when}
            post(api, source, body, generic_headers(body))
        by_id = {d.external_id: d for d in Deploy.objects.all()}
        assert (
            abs(
                (by_id["d0"].occurred_at - (timezone.now() - timedelta(minutes=10))).total_seconds()
            )
            < 5
        )
        for n in (1, 2, 3):  # future, ancient and unreadable all fall back to "now"
            assert abs((by_id[f"d{n}"].occurred_at - timezone.now()).total_seconds()) < 5

    def test_only_web_links_are_kept(self, api, user):
        source = make_source(user)
        for n, link in enumerate(
            ("https://ci.example.com/run/1", "javascript:alert(1)", "data:text/html,x")
        ):
            body = {**self.BODY, "id": f"u{n}", "url": link}
            post(api, source, body, generic_headers(body))
        urls = {d.external_id: d.url for d in Deploy.objects.all()}
        assert urls == {"u0": "https://ci.example.com/run/1", "u1": "", "u2": ""}

    def test_text_is_cleaned_and_limited(self, api, user):
        body = {"service": "my-api\x00\n\x1b[31m" + "x" * 400, "version": "v1\x07"}
        post(api, make_source(user), body, generic_headers(body))
        deploy = Deploy.objects.get()
        assert "\x00" not in deploy.service_name and "\x1b" not in deploy.service_name
        assert len(deploy.service_name) <= 150 and deploy.version == "v1"

    def test_what_we_store_is_a_small_summary_not_the_whole_payload(self, api, user):
        body = {**self.BODY, "secret_token": "do-not-keep-this", "blob": "z" * 5000}
        post(api, make_source(user), body, generic_headers(body))
        assert "do-not-keep-this" not in json.dumps(Deploy.objects.get().raw_payload)

    def test_unknown_disabled_or_mismatched_connections_are_a_plain_404(self, api, user):
        source = make_source(user)
        headers = generic_headers(self.BODY)
        assert (
            api.generic(
                "POST",
                "/api/v1/deploys/webhook/generic/nope/",
                b"{}",
                content_type="application/json",
            ).status_code
            == 404
        )
        assert post(api, source, self.BODY, headers, type="github").status_code == 404
        source.enabled = False
        source.save()
        assert post(api, source, self.BODY, headers).status_code == 404

    def test_unknown_providers_have_no_route(self):
        with pytest.raises(Resolver404):
            resolve("/api/v1/deploys/webhook/gitlab/abc/")

    def test_responses_never_echo_the_secret_or_the_token(self, api, user):
        source = make_source(user)
        resp = post(api, source, self.BODY, generic_headers(self.BODY))
        assert SECRET not in resp.content.decode() and source.token not in resp.content.decode()

    def test_it_is_rate_limited(self, api, user):
        source = make_source(user)
        codes = [post(api, source, self.BODY, {}).status_code for _ in range(125)]
        assert codes[0] == 401 and 429 in codes

    def test_it_is_not_in_the_public_docs_with_a_secret_in_the_path(self, client):
        schema = client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
        assert any("deploys/webhook" in path for path in schema["paths"])


class TestEnvironmentFilter:
    def test_other_environments_are_ignored_not_stored(self, api, user):
        source = make_source(user, environment_filter="production")
        body = {"service": "my-api", "environment": "staging"}
        resp = post(api, source, body, generic_headers(body))
        assert resp.status_code == 202 and resp.json()["status"] == "ignored"
        assert not Deploy.objects.exists()

    def test_the_matching_environment_and_unlabelled_deploys_are_kept(self, api, user):
        source = make_source(user, environment_filter="Production")
        for n, env in enumerate(("production", "")):
            body = {"service": "my-api", "environment": env, "id": f"e{n}"}
            assert post(api, source, body, generic_headers(body)).json()["status"] == "recorded"


# --- GitHub ----------------------------------------------------------------------------


class TestGithubWebhook:
    def test_a_successful_deployment_is_recorded(self, api, user):
        source = make_source(user, "github")
        resp = post(api, source, GITHUB_DEPLOY, github_headers(GITHUB_DEPLOY, "deployment_status"))
        assert resp.json()["status"] == "recorded"
        deploy = Deploy.objects.get()
        assert deploy.service_name == "my-api" and deploy.names == ["my-api", "acme/my-api"]
        assert (deploy.version, deploy.environment) == ("a" * 7, "production")
        assert deploy.url == "https://app.example.com"
        assert deploy.occurred_at == datetime(
            2026, 10, 4, 12, 0, tzinfo=UTC
        ) or deploy.occurred_at > timezone.now() - timedelta(minutes=1)

    @pytest.mark.parametrize(
        "headers",
        [{}, {"HTTP_X_HUB_SIGNATURE_256": "sha256=00", "HTTP_X_GITHUB_EVENT": "deployment_status"}],
    )
    def test_bad_signatures_are_refused(self, api, user, headers):
        assert post(api, make_source(user, "github"), GITHUB_DEPLOY, headers).status_code == 401

    def test_the_wrong_secret_is_refused(self, api, user):
        resp = post(
            api,
            make_source(user, "github"),
            GITHUB_DEPLOY,
            github_headers(GITHUB_DEPLOY, "deployment_status", secret="x"),
        )
        assert resp.status_code == 401

    def test_a_ping_is_acknowledged_and_ignored(self, api, user):
        body = {"zen": "Keep it logically awesome."}
        resp = post(api, make_source(user, "github"), body, github_headers(body, "ping"))
        assert resp.status_code == 202 and resp.json()["reason"] == "ping"

    def test_a_failed_deployment_is_not_a_deploy(self, api, user):
        body = {**GITHUB_DEPLOY, "deployment_status": {"state": "failure"}}
        resp = post(
            api, make_source(user, "github"), body, github_headers(body, "deployment_status")
        )
        assert resp.status_code == 202 and not Deploy.objects.exists()

    @pytest.mark.parametrize(
        ("name", "recorded"),
        [("Deploy to production", True), ("Release", True), ("CI tests", False), ("Lint", False)],
    )
    def test_workflow_runs_count_only_when_named_like_a_deploy(self, api, user, name, recorded):
        body = {
            "action": "completed",
            "workflow_run": {
                "id": 9,
                "run_attempt": 1,
                "name": name,
                "conclusion": "success",
                "head_sha": "b" * 40,
                "html_url": "https://github.com/acme/my-api/actions/runs/9",
                "updated_at": "2026-10-04T12:00:00Z",
            },
            "repository": {"name": "my-api", "full_name": "acme/my-api"},
        }
        resp = post(api, make_source(user, "github"), body, github_headers(body, "workflow_run"))
        assert (resp.json()["status"] == "recorded") is recorded

    def test_an_unsuccessful_workflow_run_is_ignored(self, api, user):
        body = {
            "action": "completed",
            "workflow_run": {"id": 9, "name": "Deploy", "conclusion": "failure"},
            "repository": {"name": "r"},
        }
        assert (
            post(
                api, make_source(user, "github"), body, github_headers(body, "workflow_run")
            ).status_code
            == 202
        )

    def test_other_events_are_ignored(self, api, user):
        body = {"ref": "refs/heads/main", "repository": {"name": "my-api"}}
        resp = post(api, make_source(user, "github"), body, github_headers(body, "push"))
        assert resp.status_code == 202 and "push" in resp.json()["reason"]

    def test_a_redelivery_is_not_a_second_deploy(self, api, user):
        source = make_source(user, "github")
        for _ in range(2):
            post(api, source, GITHUB_DEPLOY, github_headers(GITHUB_DEPLOY, "deployment_status"))
        assert Deploy.objects.count() == 1


# --- Vercel ----------------------------------------------------------------------------


VERCEL = {
    "id": "evt_1",
    "type": "deployment.succeeded",
    "createdAt": int(datetime(2026, 10, 4, 12, 0, tzinfo=UTC).timestamp() * 1000),
    "payload": {
        "name": "my-site",
        "target": "production",
        "project": {"id": "prj_abc"},
        "deployment": {
            "id": "dpl_1",
            "url": "my-site-git-main.vercel.app",
            "meta": {"githubCommitSha": "c" * 40},
        },
    },
}


class TestVercelWebhook:
    def test_a_succeeded_deployment_is_recorded(self, api, user):
        source = make_source(user, "vercel")
        resp = post(api, source, VERCEL, vercel_headers(VERCEL))
        assert resp.json()["status"] == "recorded"
        deploy = Deploy.objects.get()
        assert deploy.service_name == "my-site" and "prj_abc" in deploy.names
        assert deploy.environment == "production" and deploy.version == "c" * 7
        assert deploy.url == "https://my-site-git-main.vercel.app"

    def test_bad_signatures_are_refused(self, api, user):
        source = make_source(user, "vercel")
        assert post(api, source, VERCEL, {}).status_code == 401
        assert post(api, source, VERCEL, vercel_headers(VERCEL, secret="x")).status_code == 401

    def test_succeeded_and_ready_for_one_deployment_are_one_deploy(self, api, user):
        source = make_source(user, "vercel")
        ready = {**VERCEL, "type": "deployment.ready"}
        post(api, source, VERCEL, vercel_headers(VERCEL))
        assert post(api, source, ready, vercel_headers(ready)).json()["status"] == "duplicate"
        assert Deploy.objects.count() == 1

    @pytest.mark.parametrize("kind", ["deployment.created", "deployment.error", "project.created"])
    def test_other_events_are_ignored(self, api, user, kind):
        body = {**VERCEL, "type": kind}
        assert post(api, make_source(user, "vercel"), body, vercel_headers(body)).status_code == 202

    def test_a_preview_keeps_its_environment_so_a_filter_can_drop_it(self, api, user):
        body = {**VERCEL, "payload": {**VERCEL["payload"], "target": "preview"}}
        source = make_source(user, "vercel", environment_filter="production")
        assert post(api, source, body, vercel_headers(body)).status_code == 202


# --- Render ----------------------------------------------------------------------------


RENDER = {
    "type": "deploy_ended",
    "timestamp": datetime(2026, 10, 4, 12, 0, tzinfo=UTC).isoformat(),
    "data": {"id": "dep-1", "serviceId": "srv-9", "serviceName": "my-api", "status": "succeeded"},
}


class TestRenderWebhook:
    def test_a_succeeded_deploy_is_recorded(self, api, user):
        source = make_source(user, "render")
        resp = post(api, source, RENDER, render_headers(RENDER))
        assert resp.json()["status"] == "recorded"
        deploy = Deploy.objects.get()
        assert deploy.service_name == "my-api" and deploy.names == ["my-api", "srv-9"]

    def test_a_secret_in_whsec_form_is_decoded(self, api, user):
        secret = "whsec_" + base64.b64encode(b"a-raw-signing-key").decode()
        source = make_source(user, "render", secret=secret)
        assert (
            post(api, source, RENDER, render_headers(RENDER, secret=secret)).json()["status"]
            == "recorded"
        )

    def test_bad_signatures_are_refused(self, api, user):
        source = make_source(user, "render")
        assert post(api, source, RENDER, {}).status_code == 401
        assert post(api, source, RENDER, render_headers(RENDER, secret="other")).status_code == 401

    def test_an_old_timestamp_is_refused_even_with_a_valid_signature(self, api, user):
        stale = render_headers(RENDER, stamp=int(time.time()) - 3600)
        assert post(api, make_source(user, "render"), RENDER, stale).status_code == 401

    def test_a_rotated_secret_can_sign_alongside_an_old_one(self, api, user):
        headers = render_headers(RENDER, extra_signatures=["bm90LXRoZS1yaWdodC1vbmU="])
        assert (
            post(api, make_source(user, "render"), RENDER, headers).json()["status"] == "recorded"
        )

    def test_failed_deploys_and_other_events_are_ignored(self, api, user):
        source = make_source(user, "render")
        failed = {**RENDER, "data": {**RENDER["data"], "status": "failed"}}
        other = {**RENDER, "type": "service_suspended"}
        assert post(api, source, failed, render_headers(failed)).status_code == 202
        assert post(api, source, other, render_headers(other)).status_code == 202
        assert not Deploy.objects.exists()

    def test_without_a_service_name_the_service_id_is_used(self, api, user):
        body = {**RENDER, "data": {"id": "dep-2", "serviceId": "srv-9", "status": "succeeded"}}
        post(api, make_source(user, "render"), body, render_headers(body))
        assert Deploy.objects.get().service_name == "srv-9"


class TestParserHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-10-04T12:00:00Z", datetime(2026, 10, 4, 12, tzinfo=UTC)),
            (1791115200, datetime(2026, 10, 4, 12, tzinfo=UTC)),
            (1791115200000, datetime(2026, 10, 4, 12, tzinfo=UTC)),
            ("nope", None),
            (None, None),
            (True, None),
        ],
    )
    def test_parse_time(self, value, expected):
        assert parsers.parse_time(value) == expected

    def test_short_ref(self):
        assert parsers.short_ref("a" * 40) == "a" * 7
        assert parsers.short_ref("v1.2.3") == "v1.2.3"
        assert parsers.short_ref(None) == ""


# --- linking a deploy to an incident ---------------------------------------------------


class TestCorrelation:
    def test_an_incident_soon_after_a_deploy_is_linked_to_it(self, user):
        monitor = make_monitor(user)
        deploy = make_deploy(user, minutes_ago=3)
        incident = open_incident(monitor)
        assert incident.deploy == deploy
        event = IncidentEvent.objects.get(incident=incident, kind="deploy_linked")
        assert (
            event.metadata["service"] == "my-api" and 170 <= event.metadata["seconds_before"] <= 190
        )

    def test_a_deploy_outside_the_window_is_not_blamed(self, user):
        monitor = make_monitor(user)
        make_deploy(user, minutes_ago=7)
        assert open_incident(monitor).deploy is None

    def test_a_deploy_after_the_incident_is_not_the_cause(self, user):
        monitor = make_monitor(user)
        incident = open_incident(monitor, minutes_ago=10)
        make_deploy(user, minutes_ago=2)  # the fix going out, not the cause
        assert not Incident.objects.get(pk=incident.pk).deploy_id

    def test_the_window_is_configurable(self, user, settings):
        settings.DEPLOY_CORRELATION_WINDOW_SECONDS = 900
        monitor = make_monitor(user)
        make_deploy(user, minutes_ago=12)
        assert open_incident(monitor).deploy is not None

    def test_another_service_is_not_blamed(self, user):
        monitor = make_monitor(user, tag="my-api")
        make_deploy(user, service="billing", minutes_ago=2)
        assert open_incident(monitor).deploy is None

    def test_an_untagged_monitor_is_never_linked(self, user):
        monitor = make_monitor(user, tag="")
        make_deploy(user, service="", minutes_ago=2, names=[""])
        make_deploy(user, minutes_ago=2)
        assert open_incident(monitor).deploy is None

    def test_matching_ignores_case_and_spaces(self, user):
        monitor = make_monitor(user, tag="  My-API ")
        make_deploy(user, service="my-api", minutes_ago=2)
        assert open_incident(monitor).deploy is not None

    def test_a_repo_full_name_matches_too(self, user):
        monitor = make_monitor(user, tag="Acme/My-API")
        make_deploy(user, service="my-api", names=["my-api", "acme/my-api"], minutes_ago=2)
        assert open_incident(monitor).deploy is not None

    def test_the_latest_matching_deploy_wins(self, user):
        monitor = make_monitor(user)
        make_deploy(user, minutes_ago=4, version="old")
        newest = make_deploy(user, minutes_ago=1, version="new")
        make_deploy(user, service="billing", minutes_ago=0)
        assert open_incident(monitor).deploy == newest

    def test_other_users_deploys_never_link(self, user, make_user):
        other = make_user("bob@example.com")
        monitor = make_monitor(user)
        make_deploy(other, minutes_ago=2)
        assert open_incident(monitor).deploy is None

    def test_a_deploy_that_arrives_late_still_links_when_it_happened_just_before(self, user):
        monitor = make_monitor(user)
        incident = open_incident(monitor, minutes_ago=2)  # opened before the webhook landed
        assert incident.deploy is None
        deploy = make_deploy(user, minutes_ago=3)  # it happened a minute before the incident
        deploy_services.link_deploy_to_incidents(deploy)
        incident.refresh_from_db()
        assert incident.deploy == deploy
        assert IncidentEvent.objects.filter(incident=incident, kind="deploy_linked").count() == 1

    def test_ingesting_a_late_deploy_links_it(self, api, user):
        source = make_source(user)
        monitor = make_monitor(user)
        incident = open_incident(monitor, minutes_ago=1)
        body = {
            "service": "my-api",
            "occurred_at": (timezone.now() - timedelta(minutes=2)).isoformat(),
        }
        post(api, source, body, generic_headers(body))
        incident.refresh_from_db()
        assert incident.deploy is not None and incident.deploy.service_name == "my-api"

    def test_a_closer_late_deploy_replaces_a_farther_one_but_not_the_reverse(self, user):
        monitor = make_monitor(user)
        far = make_deploy(user, minutes_ago=4, version="far")
        incident = open_incident(monitor, minutes_ago=0)
        assert incident.deploy == far
        near = make_deploy(user, minutes_ago=2, version="near")
        deploy_services.link_deploy_to_incidents(near)
        incident.refresh_from_db()
        assert incident.deploy == near
        earlier = make_deploy(user, minutes_ago=3, version="earlier")
        deploy_services.link_deploy_to_incidents(earlier)
        incident.refresh_from_db()
        assert incident.deploy == near

    def test_a_late_deploy_for_an_old_incident_does_not_link(self, user):
        monitor = make_monitor(user)
        incident = open_incident(monitor, minutes_ago=60)
        deploy = make_deploy(user, minutes_ago=3)
        deploy_services.link_deploy_to_incidents(deploy)
        incident.refresh_from_db()
        assert incident.deploy is None

    def test_a_replayed_webhook_does_not_link_twice(self, api, user):
        source = make_source(user)
        monitor = make_monitor(user)
        incident = open_incident(monitor, minutes_ago=1)
        body = {
            "service": "my-api",
            "id": "d1",
            "occurred_at": (timezone.now() - timedelta(minutes=2)).isoformat(),
        }
        for _ in range(3):
            post(api, source, body, generic_headers(body))
        assert IncidentEvent.objects.filter(incident=incident, kind="deploy_linked").count() == 1

    def test_deleting_a_deploy_leaves_the_incident(self, user):
        monitor = make_monitor(user)
        deploy = make_deploy(user, minutes_ago=2)
        incident = open_incident(monitor)
        deploy.delete()
        incident.refresh_from_db()
        assert incident.deploy is None and incident.pk


# --- root cause ------------------------------------------------------------------------


USUAL = {"dns": 20, "tcp": 40, "tls": 50, "server": 200}


def check(**fields):
    base = {
        "status_detail": "status_mismatch",
        "dns_ms": 20,
        "tcp_ms": 40,
        "tls_ms": 50,
        "ttfb_ms": 200,
    }
    base.update(fields)
    return Check(success=False, **base)


class TestRootCause:
    @pytest.mark.parametrize(
        ("detail", "stage"),
        [
            ("dns_error", "dns"),
            ("connection_refused", "tcp"),
            ("connection_error", "tcp"),
            ("tls_error", "tls"),
        ],
    )
    def test_some_failures_name_their_own_stage(self, detail, stage):
        result = rootcause.analyse(check(status_detail=detail, ttfb_ms=None), USUAL)
        assert (result["stage"], result["basis"]) == (stage, "failure")

    def test_a_much_slower_stage_is_the_cause(self):
        result = rootcause.analyse(check(tls_ms=900), USUAL)
        assert (result["stage"], result["basis"]) == ("tls", "timing")
        assert [s["regressed"] for s in result["stages"]] == [False, False, True, False]

    def test_the_stage_that_lost_the_most_time_wins(self):
        result = rootcause.analyse(check(tcp_ms=400, ttfb_ms=3000), USUAL)
        assert result["stage"] == "server"
        assert sum(s["regressed"] for s in result["stages"]) == 2

    def test_a_small_absolute_change_is_not_a_regression(self):
        result = rootcause.analyse(check(dns_ms=90), USUAL)  # 4.5x slower, but only 70 ms
        assert result["stages"][0]["regressed"] is False

    def test_a_modest_ratio_is_not_a_regression(self):
        result = rootcause.analyse(check(ttfb_ms=350), USUAL)  # +150 ms but only 1.75x
        assert result["stages"][3]["regressed"] is False

    def test_a_server_that_answers_with_an_error_is_the_server(self):
        result = rootcause.analyse(check(), USUAL)
        assert (result["stage"], result["basis"]) == ("server", "response")

    @pytest.mark.parametrize("detail", ["keyword_missing", "keyword_found"])
    def test_content_failures_are_the_servers_too(self, detail):
        assert rootcause.analyse(check(status_detail=detail), USUAL)["stage"] == "server"

    def test_without_history_only_failures_that_name_a_stage_are_certain(self):
        nothing = dict.fromkeys(USUAL)
        assert rootcause.analyse(check(status_detail="dns_error"), nothing)["stage"] == "dns"
        assert rootcause.analyse(check(tls_ms=5000), nothing)["basis"] == "response"

    def test_a_timeout_with_no_timings_is_not_guessed_at(self):
        assert (
            rootcause.analyse(
                check(status_detail="timeout", dns_ms=None, tcp_ms=None, tls_ms=None, ttfb_ms=None),
                USUAL,
            )
            == {}
        )

    def test_a_timeout_with_partial_timings_says_it_doesnt_know(self):
        result = rootcause.analyse(check(status_detail="timeout", tls_ms=None, ttfb_ms=None), USUAL)
        assert (result["stage"], result["basis"]) == ("", "unknown")
        assert result["stages"][0]["ms"] == 20

    def test_baseline_is_the_30_day_average_from_rollups(self, user):
        monitor = make_monitor(user)
        today = timezone.now().date()
        for n, (count, dns, tcp, tls, ttfb) in enumerate(
            [(100, 2000, 4000, 5000, 20000), (100, 2000, 4000, 5000, 20000)]
        ):
            CheckRollupDaily.objects.create(
                monitor=monitor,
                day=today - timedelta(days=n + 1),
                timing_count=count,
                dns_sum_ms=dns,
                tcp_sum_ms=tcp,
                tls_sum_ms=tls,
                ttfb_sum_ms=ttfb,
            )
        CheckRollupDaily.objects.create(
            monitor=monitor,
            day=today - timedelta(days=45),
            timing_count=100,
            dns_sum_ms=10**6,
            tcp_sum_ms=10**6,
            tls_sum_ms=10**6,
            ttfb_sum_ms=10**6,
        )
        assert rootcause.baseline(monitor) == USUAL

    def test_baseline_without_history_is_empty(self, user):
        assert rootcause.baseline(make_monitor(user)) == dict.fromkeys(rootcause.STAGES)

    def test_opening_an_incident_stores_the_stage_and_figures(self, user):
        monitor = make_monitor(user)
        today = timezone.now().date()
        CheckRollupDaily.objects.create(
            monitor=monitor,
            day=today - timedelta(days=1),
            timing_count=100,
            dns_sum_ms=2000,
            tcp_sum_ms=4000,
            tls_sum_ms=5000,
            ttfb_sum_ms=20000,
        )
        incident = open_incident(monitor, dns_ms=20, tcp_ms=40, tls_ms=1200, ttfb_ms=210)
        assert incident.root_cause_stage == "tls"
        assert incident.root_cause["basis"] == "timing"
        assert incident.root_cause["stages"][2] == {
            "stage": "tls",
            "ms": 1200,
            "baseline_ms": 50,
            "regressed": True,
        }

    def test_other_monitor_types_have_no_stage(self, user):
        monitor = make_monitor(user, type="port", target="db.example.com", tag="")
        incident = open_incident(monitor, status_detail="connection_refused")
        assert incident.root_cause_stage == "" and incident.root_cause == {}

    def test_explaining_never_blocks_the_incident_or_the_alert(self, user, monkeypatch):
        monitor = make_monitor(user)
        contact = AlertContact.objects.create(
            user=user,
            name="Ops",
            channel="slack",
            config={"webhook_url": "https://hooks.slack.com/services/T/B/x"},
        )
        monitor.alert_contacts.add(contact)

        def boom(*a, **k):
            raise RuntimeError("bug")

        monkeypatch.setattr(deploy_services, "correlate_incident", boom)
        incident = open_incident(monitor)
        assert incident.pk and NotificationDelivery.objects.filter(incident=incident).count() == 1


# --- alerts ----------------------------------------------------------------------------


class TestAlertsMentionTheDeploy:
    def test_the_down_alert_says_what_it_followed_and_where(self, user):
        monitor = make_monitor(user)
        make_deploy(user, minutes_ago=2, version="abc1234")
        incident = open_incident(monitor, ttfb_ms=None, status_detail="connection_refused")
        payload = messages.build("opened", monitor, incident)
        assert "Started 2 min after a deploy of my-api (abc1234)." in payload["summary"]
        assert "Most likely stage: the connection." in payload["summary"]
        assert payload["incident"]["deploy"]["version"] == "abc1234"
        assert payload["incident"]["root_cause_stage"] == "tcp"

    def test_without_either_the_alert_is_unchanged(self, user):
        monitor = make_monitor(user, type="port", target="db.example.com", tag="")
        incident = open_incident(monitor)
        payload = messages.build("opened", monitor, incident)
        assert payload["summary"] == "Unexpected status 503."
        assert payload["incident"]["deploy"] is None

    def test_the_recovery_alert_stays_short(self, user):
        monitor = make_monitor(user)
        make_deploy(user, minutes_ago=2)
        incident = open_incident(monitor)
        assert "deploy" not in messages.build("resolved", monitor, incident)["summary"]

    def test_the_sent_alert_carries_it(self, user, monkeypatch):
        sent = []
        from notifications.channels import CHANNELS

        monkeypatch.setattr(
            CHANNELS["slack"], "send", lambda config, payload, delivery_id: sent.append(payload)
        )
        monitor = make_monitor(user)
        contact = AlertContact.objects.create(
            user=user,
            name="Ops",
            channel="slack",
            config={"webhook_url": "https://hooks.slack.com/services/T/B/x"},
        )
        monitor.alert_contacts.add(contact)
        make_deploy(user, minutes_ago=1, version="v9")
        open_incident(monitor)
        dispatcher.run_notifications_once(workers=1)
        assert "after a deploy of my-api (v9)" in sent[0]["summary"]


# --- the API ---------------------------------------------------------------------------


SOURCES = "/api/v1/deploy-sources/"


@pytest.fixture
def other_api(make_user, sign_in):
    client = APIClient(HTTP_ORIGIN=ORIGIN)
    person = make_user("mallory@example.com")
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {sign_in(client, person.email).json()['access_token']}"
    )
    client.user = person
    return client


class TestDeploySourceApi:
    def test_a_generated_secret_is_shown_once(self, auth_api):
        resp = auth_api.post(SOURCES, {"name": "CI", "type": "generic"}, format="json")
        assert resp.status_code == 201
        body = resp.json()
        assert len(body["secret"]) >= 32
        source = DeploySource.objects.get()
        assert (
            body["webhook_url"]
            == f"http://localhost:8000/api/v1/deploys/webhook/generic/{source.token}/"
        )
        assert source.secret == body["secret"]
        assert auth_api.get(f"{SOURCES}{body['id']}/").json()["secret"] is None
        assert all(s["secret"] is None for s in auth_api.get(SOURCES).json()["results"])

    def test_github_generates_one_too(self, auth_api):
        body = auth_api.post(SOURCES, {"name": "Repo", "type": "github"}, format="json").json()
        assert body["secret"]

    @pytest.mark.parametrize("kind", ["vercel", "render"])
    def test_vercel_and_render_use_the_providers_secret(self, auth_api, kind):
        assert auth_api.post(SOURCES, {"name": "x", "type": kind}, format="json").status_code == 400
        resp = auth_api.post(
            SOURCES,
            {"name": "x", "type": kind, "signing_secret": "from-the-provider"},
            format="json",
        )
        assert resp.status_code == 201 and resp.json()["secret"] is None
        assert DeploySource.objects.get().secret == "from-the-provider"
        assert "from-the-provider" not in resp.content.decode()

    def test_generated_kinds_refuse_a_chosen_secret(self, auth_api):
        resp = auth_api.post(
            SOURCES, {"name": "x", "type": "generic", "signing_secret": "weak"}, format="json"
        )
        assert resp.status_code == 400 and "signing_secret" in resp.json()["field_errors"]

    def test_rotating_a_generated_secret(self, auth_api):
        created = auth_api.post(SOURCES, {"name": "CI", "type": "generic"}, format="json").json()
        rotated = auth_api.post(f"{SOURCES}{created['id']}/rotate-secret/").json()
        assert rotated["secret"] and rotated["secret"] != created["secret"]
        assert auth_api.get(f"{SOURCES}{created['id']}/").json()["secret"] is None

    def test_rotating_where_the_provider_issues_the_secret_is_refused(self, auth_api):
        created = auth_api.post(
            SOURCES, {"name": "V", "type": "vercel", "signing_secret": "abc"}, format="json"
        ).json()
        assert auth_api.post(f"{SOURCES}{created['id']}/rotate-secret/").status_code == 400

    def test_a_replaced_secret_works_for_the_next_webhook_and_not_the_old(
        self, auth_api, api, user
    ):
        created = auth_api.post(
            SOURCES, {"name": "V", "type": "vercel", "signing_secret": "old-secret"}, format="json"
        ).json()
        auth_api.patch(
            f"{SOURCES}{created['id']}/", {"signing_secret": "new-secret"}, format="json"
        )
        source = DeploySource.objects.get()
        anon = APIClient()
        assert (
            post(anon, source, VERCEL, vercel_headers(VERCEL, secret="old-secret")).status_code
            == 401
        )
        assert (
            post(anon, source, VERCEL, vercel_headers(VERCEL, secret="new-secret")).status_code
            == 200
        )

    def test_edit_name_environment_and_switch_off(self, auth_api):
        created = auth_api.post(SOURCES, {"name": "CI", "type": "generic"}, format="json").json()
        resp = auth_api.patch(
            f"{SOURCES}{created['id']}/",
            {"name": "Prod CI", "environment_filter": "production", "enabled": False},
            format="json",
        )
        assert resp.status_code == 200
        assert (resp.json()["name"], resp.json()["environment_filter"], resp.json()["enabled"]) == (
            "Prod CI",
            "production",
            False,
        )

    def test_the_type_cannot_change(self, auth_api):
        created = auth_api.post(SOURCES, {"name": "CI", "type": "generic"}, format="json").json()
        assert (
            auth_api.patch(
                f"{SOURCES}{created['id']}/", {"type": "github"}, format="json"
            ).status_code
            == 400
        )

    def test_a_name_is_needed(self, auth_api):
        assert (
            auth_api.post(SOURCES, {"name": " ", "type": "generic"}, format="json").status_code
            == 400
        )
        assert (
            auth_api.post(SOURCES, {"name": "x", "type": "gitlab"}, format="json").status_code
            == 400
        )

    def test_the_limit(self, auth_api, settings):
        settings.MAX_DEPLOY_SOURCES_PER_USER = 1
        assert (
            auth_api.post(SOURCES, {"name": "a", "type": "generic"}, format="json").status_code
            == 201
        )
        assert (
            auth_api.post(SOURCES, {"name": "b", "type": "generic"}, format="json").status_code
            == 403
        )

    def test_deleting_keeps_the_deploys_it_reported(self, auth_api, user):
        source = make_source(user)
        make_deploy(user)
        Deploy.objects.update(source=source)
        assert auth_api.delete(f"{SOURCES}{source.id}/").status_code == 204
        deploy = Deploy.objects.get()
        assert deploy.source is None and deploy.source_name == "CI"
        # and the old webhook URL is dead
        assert (
            post(
                APIClient(), source, {"service": "x"}, generic_headers({"service": "x"})
            ).status_code
            == 404
        )

    def test_counts_deploys_per_connection(self, auth_api, user):
        source = make_source(user)
        for n in range(3):
            Deploy.objects.create(
                user=user,
                source=source,
                source_type="generic",
                source_name="x",
                external_id=f"c{n}",
                service_name="s",
                names=["s"],
                occurred_at=timezone.now(),
            )
        assert auth_api.get(SOURCES).json()["results"][0]["deploy_count"] == 3

    def test_other_users_connections_are_invisible(self, auth_api, other_api, user):
        mine = make_source(user)
        assert other_api.get(SOURCES).json()["results"] == []
        for method in (other_api.get, other_api.patch, other_api.delete):
            assert method(f"{SOURCES}{mine.id}/").status_code == 404
        assert other_api.post(f"{SOURCES}{mine.id}/rotate-secret/").status_code == 404

    def test_requires_sign_in(self, api):
        assert api.get(SOURCES).status_code == 401

    def test_creating_and_deleting_are_audited(self, auth_api, user):
        created = auth_api.post(SOURCES, {"name": "CI", "type": "generic"}, format="json").json()
        auth_api.delete(f"{SOURCES}{created['id']}/")
        actions = set(AuditLog.objects.filter(user=user).values_list("action", flat=True))
        assert {"deploy_source_created", "deploy_source_deleted"} <= actions

    def test_the_secret_never_appears_in_a_later_response(self, auth_api):
        created = auth_api.post(SOURCES, {"name": "CI", "type": "generic"}, format="json").json()
        raw = (
            auth_api.get(SOURCES).content.decode()
            + auth_api.get(f"{SOURCES}{created['id']}/").content.decode()
        )
        assert created["secret"] not in raw


class TestDeployListApi:
    def test_newest_first_and_only_mine(self, auth_api, user, other_api):
        old = make_deploy(user, minutes_ago=60, version="old")
        new = make_deploy(user, minutes_ago=5, version="new")
        make_deploy(other_api.user, minutes_ago=1, version="theirs")
        rows = auth_api.get("/api/v1/deploys/").json()["results"]
        assert [r["id"] for r in rows] == [str(new.id), str(old.id)]
        assert "raw_payload" not in rows[0] and "names" not in rows[0]

    def test_filter_by_service(self, auth_api, user):
        make_deploy(user, service="my-api")
        make_deploy(user, service="billing")
        rows = auth_api.get("/api/v1/deploys/?service=BILLING").json()["results"]
        assert [r["service_name"] for r in rows] == ["billing"]

    def test_requires_sign_in(self, api):
        assert api.get("/api/v1/deploys/").status_code == 401


class TestMonitorTag:
    def test_can_be_set_on_create_and_edit(self, auth_api):
        created = auth_api.post(
            "/api/v1/monitors/",
            {
                "name": "Site",
                "type": "http",
                "target": "https://example.com",
                "deploy_service": "my-api",
            },
            format="json",
        ).json()
        assert created["deploy_service"] == "my-api"
        edited = auth_api.patch(
            f"/api/v1/monitors/{created['id']}/", {"deploy_service": "acme/my-api"}, format="json"
        ).json()
        assert edited["deploy_service"] == "acme/my-api"
        assert (
            auth_api.get("/api/v1/monitors/").json()["results"][0]["deploy_service"]
            == "acme/my-api"
        )

    def test_defaults_to_blank(self, auth_api):
        created = auth_api.post(
            "/api/v1/monitors/",
            {"name": "Site", "type": "http", "target": "https://example.com"},
            format="json",
        ).json()
        assert created["deploy_service"] == ""

    def test_changing_it_does_not_close_an_open_incident(self, auth_api, user):
        monitor = make_monitor(user, tag="")
        incident = open_incident(monitor)
        auth_api.patch(
            f"/api/v1/monitors/{monitor.id}/", {"deploy_service": "my-api"}, format="json"
        )
        incident.refresh_from_db()
        assert incident.ended_at is None

    def test_too_long_is_refused(self, auth_api, user):
        monitor = make_monitor(user)
        assert (
            auth_api.patch(
                f"/api/v1/monitors/{monitor.id}/", {"deploy_service": "x" * 101}, format="json"
            ).status_code
            == 400
        )


class TestIncidentApi:
    def test_the_detail_shows_the_deploy_and_the_root_cause(self, auth_api, user):
        monitor = make_monitor(user)
        make_deploy(user, minutes_ago=2, version="abc1234", url="https://ci.example.com/1")
        incident = open_incident(monitor, dns_ms=20, tcp_ms=40, tls_ms=1500, ttfb_ms=200)
        body = auth_api.get(f"/api/v1/incidents/{incident.id}/").json()
        assert body["deploy"]["version"] == "abc1234" and body["deploy"]["service_name"] == "my-api"
        assert 100 <= body["deploy"]["seconds_before_incident"] <= 140
        assert body["deploy"]["url"] == "https://ci.example.com/1"
        assert body["root_cause_stage"] == "server" and body["root_cause"]["basis"] == "response"
        assert {s["stage"] for s in body["root_cause"]["stages"]} == {"dns", "tcp", "tls", "server"}
        assert any(e["kind"] == "deploy_linked" for e in body["events"])

    def test_the_list_flags_incidents_that_followed_a_deploy(self, auth_api, user):
        monitor = make_monitor(user)
        make_deploy(user, minutes_ago=2)
        open_incident(monitor)
        row = auth_api.get("/api/v1/incidents/").json()["results"][0]
        assert row["deploy"]["service_name"] == "my-api"

    def test_an_incident_without_one_says_null(self, auth_api, user):
        incident = open_incident(make_monitor(user))
        body = auth_api.get(f"/api/v1/incidents/{incident.id}/").json()
        assert body["deploy"] is None

    def test_the_postmortem_starts_from_what_we_know(self, auth_api, user):
        monitor = make_monitor(user)
        make_deploy(user, minutes_ago=2, version="abc1234")
        incident = open_incident(monitor, status_detail="connection_refused", ttfb_ms=None)
        incident_services.close_incident(monitor, kind="resolved", resolution="recovered")
        md = auth_api.post(f"/api/v1/incidents/{incident.id}/postmortem/").json()["markdown"]
        assert "after a deploy of my-api (abc1234)" in md
        assert "The request went wrong at the connection." in md

    def test_the_postmortem_without_any_is_unchanged(self, auth_api, user):
        monitor = make_monitor(user, type="port", target="db.example.com", tag="")
        incident = open_incident(monitor)
        incident_services.close_incident(monitor, kind="resolved", resolution="recovered")
        md = auth_api.post(f"/api/v1/incidents/{incident.id}/postmortem/").json()["markdown"]
        assert "## Root cause\n\n_What actually went wrong, and why?_" in md


class TestFleetFigure:
    def test_how_many_incidents_followed_a_deploy(self, auth_api, user):
        shop = make_monitor(user, "Shop")
        other = make_monitor(user, "Blog", tag="blog")
        make_deploy(user, minutes_ago=2)
        for monitor, minutes in ((shop, 0), (other, 0)):
            open_incident(monitor, minutes_ago=minutes)
        summary = auth_api.get("/api/v1/analytics/fleet/?range=7d").json()["summary"]
        assert summary["incidents"] == 2
        assert summary["deploy_linked_incidents"] == 1 and summary["deploy_linked_percent"] == 50.0

    def test_no_incidents_means_no_percentage(self, auth_api):
        summary = auth_api.get("/api/v1/analytics/fleet/").json()["summary"]
        assert summary["deploy_linked_incidents"] == 0 and summary["deploy_linked_percent"] is None
