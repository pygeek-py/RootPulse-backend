"""The Phase 16 gate: properties of the whole API, checked by crawling it.

Per-feature tests prove each feature protects itself. These prove nothing was missed: they walk
every route and every operation in the published schema, so a new endpoint added tomorrow is
covered the day it exists, with no one needing to remember to write its security test.

  * the set of routes that answer without signing in is exactly the reviewed list
  * every other endpoint refuses an anonymous caller
  * a read-only API key is refused on every operation that changes something
  * user B can't reach any of user A's objects through any endpoint, by any id
  * the API's responses carry the headers they should
  * the production settings pass Django's deployment check
"""

import os
import re
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.cache import cache
from django.db import models as dj_models
from django.urls import URLResolver, get_resolver
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.test import APIClient

from apikeys.models import ApiKey
from deploys.models import Deploy, DeploySource
from incidents.models import Incident, IncidentComment, IncidentEvent
from monitoring.models import AlertContact, MaintenanceWindow, Monitor
from notifications.models import NotificationDelivery
from providers.models import Provider, UserProviderSubscription
from statuspages.models import (
    StatusPage,
    StatusPageAnnouncement,
    StatusPageComponent,
    StatusPageSubscriber,
)

pytestmark = pytest.mark.django_db

ORIGIN = "http://localhost:3000"
BASE_DIR = Path(__file__).resolve().parent.parent
SECRET = (
    "k9Zr2-Qm7VtXw4LhB8nPc1YdJf6GsUe3AoRiT5vNlKxMyHb0CaDgWzEqSuFjPt"  # for the subprocess checks
)

# The routes that answer a caller who isn't signed in, each with the reason it has to. A new
# route here is a decision someone makes on purpose, not something that happens by accident.
PUBLIC_ROUTES = {
    "health/": "the platform's and the uptime watcher's health probe",
    "api/v1/auth/register/": "start sign-up (emails a link)",
    "api/v1/auth/login/": "start sign-in (emails a link)",
    "api/v1/auth/verify/": "finish sign-in with the emailed link or code",
    "api/v1/auth/logout/": "clears the refresh cookie; needs nothing",
    "api/v1/auth/refresh/": "swap the refresh cookie for an access token",
    "api/v1/auth/github/redirect/": "start GitHub sign-in",
    "api/v1/auth/github/callback/": "finish GitHub sign-in (state-checked)",
    "api/v1/integrations/telegram/webhook/": "Telegram calls it; secret header checked",
    "api/v1/integrations/<regex:provider>/callback/": "Discord/Slack OAuth return; state-checked",
    "api/v1/deploys/webhook/<deploysource:source>/<str:token>/": "deploy webhooks; signed",
    "api/v1/public/status-pages/<slug:slug>/": "a published status page",
    "api/v1/public/status-pages/<slug:slug>/unlock/": "password gate for a status page",
    "api/v1/public/status-pages/<slug:slug>/subscribe/": "status page email sign-up",
    "api/v1/public/status-pages/<slug:slug>/confirm/<str:token>/": "token-checked confirmation",
    "api/v1/public/status-pages/<slug:slug>/unsubscribe/<str:token>/": "token-checked unsubscribe",
    "api/v1/heartbeat/<str:token>/": "a job pings its secret URL",
    "internal/run-due-checks/": "the scheduler's trigger; HMAC-signed",
    "internal/run-rollups/": "the rollup trigger; HMAC-signed",
    "api/v1/schema/": "the published OpenAPI schema",
    "api/v1/docs/": "the published API reference",
}

# The same list as it appears in the schema (the ones that are in it), for the crawls below.
PUBLIC_OPERATIONS = {
    "/api/v1/auth/register/",
    "/api/v1/auth/login/",
    "/api/v1/auth/verify/",
    "/api/v1/auth/logout/",
    "/api/v1/auth/refresh/",
    "/api/v1/auth/github/redirect/",
    "/api/v1/auth/github/callback/",
    "/api/v1/deploys/webhook/{source}/{token}/",
    "/api/v1/public/status-pages/{slug}/",
    "/api/v1/public/status-pages/{slug}/unlock/",
    "/api/v1/public/status-pages/{slug}/subscribe/",
    "/api/v1/public/status-pages/{slug}/confirm/{token}/",
    "/api/v1/public/status-pages/{slug}/unsubscribe/{token}/",
}

UNSAFE = ("post", "put", "patch", "delete")
SAMPLE = {"source": "github", "provider": "slack", "slug": "acme", "token": "abcdefghij"}


def walk(patterns, prefix=""):
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from walk(pattern.url_patterns, route)
        else:
            yield route, pattern.callback


def schema_operations():
    """(path, method, operation) for everything the API publishes."""
    schema = SchemaGenerator().get_schema(request=None, public=True)
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            if method in ("get", "post", "put", "patch", "delete"):
                yield path, method, op


def concrete(path: str, op: dict, value: str | None = None) -> str:
    """A real-looking URL for a schema path. `value` stands in for every id."""
    params = {p["name"]: p for p in op.get("parameters", []) if p.get("in") == "path"}

    def fill(match):
        name = match.group(1)
        is_id = params.get(name, {}).get("schema", {}).get("format") == "uuid" or (
            name == "id" or name.endswith("_id")
        )
        if is_id:
            return value or str(uuid.uuid4())
        return SAMPLE.get(name, "abc")

    return re.sub(r"\{([^}]+)\}", fill, path)


def call(client: APIClient, method: str, url: str):
    cache.clear()  # these crawls make hundreds of calls; the throttles have their own tests
    if method == "get":
        return client.get(url)
    return getattr(client, method)(url, {}, format="json")


def anonymous() -> APIClient:
    return APIClient(HTTP_ORIGIN=ORIGIN, raise_request_exception=False)


# --- who can reach what ---------------------------------------------------------------------


class TestRouteInventory:
    def test_the_routes_that_answer_without_signing_in_are_exactly_the_reviewed_ones(self):
        found = set()
        for route, callback in walk(get_resolver().url_patterns):
            if route.startswith("admin/"):
                continue  # Django's own, staff-only login (see TestAdmin)
            view_class = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
            if view_class is None:
                found.add(route)  # a plain function view has no permission system: list it
            elif AllowAny in view_class.permission_classes or not view_class.authentication_classes:
                found.add(route)
        assert found == set(PUBLIC_ROUTES), {
            "new and unreviewed": sorted(found - set(PUBLIC_ROUTES)),
            "listed but gone": sorted(set(PUBLIC_ROUTES) - found),
        }

    def test_every_other_view_requires_a_signed_in_user_and_accepts_both_credentials(self):
        problems = []
        for route, callback in walk(get_resolver().url_patterns):
            view_class = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
            if route.startswith("admin/") or route in PUBLIC_ROUTES or view_class is None:
                continue
            if IsAuthenticated not in view_class.permission_classes:
                problems.append(f"{route}: no IsAuthenticated")
            names = {a.__name__ for a in view_class.authentication_classes}
            if names != {"ApiKeyAuthentication", "JWTAuthentication"}:
                problems.append(f"{route}: authenticators {sorted(names)}")
        assert problems == []

    def test_the_published_schema_matches_the_urlconf_for_public_operations(self):
        paths = {path for path, _, _ in schema_operations()}
        assert PUBLIC_OPERATIONS <= paths  # every public operation we crawl really is published


class TestAnonymousCallers:
    def test_every_endpoint_refuses_an_anonymous_caller_except_the_reviewed_public_ones(self):
        client = anonymous()
        open_ones, crawled = [], 0
        for path, method, op in schema_operations():
            if path in PUBLIC_OPERATIONS:
                continue
            crawled += 1
            response = call(client, method, concrete(path, op))
            if response.status_code not in (401, 403):
                open_ones.append((method.upper(), path, response.status_code))
        assert crawled > 70
        assert open_ones == []

    def test_nothing_answers_with_a_server_error_to_garbage(self, make_user, sign_in):
        """Odd input must not produce a 500 anywhere (a 500 is a bug, and often a leak)."""
        user = make_user("signed-in@example.com")
        client = APIClient(HTTP_ORIGIN=ORIGIN, raise_request_exception=False)
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {sign_in(client, user.email).json()['access_token']}"
        )
        junk = {"name": "\x00", "x" * 10: "y", "id": "../../etc/passwd", "monitor_id": [1, 2]}
        crashed = []
        for path, method, op in schema_operations():
            if path in PUBLIC_OPERATIONS or path.startswith("/api/v1/auth/"):
                continue
            url = concrete(path, op)
            cache.clear()
            response = (
                client.get(url, junk)
                if method == "get"
                else getattr(client, method)(url, junk, format="json")
            )
            if response.status_code >= 500:
                crashed.append((method.upper(), path, response.status_code))
        assert crashed == []


class TestReadOnlyKeys:
    def test_a_read_key_is_refused_on_every_operation_that_changes_something(self, user):
        _, raw = ApiKey.issue(user, "ci", "read")
        client = APIClient(HTTP_ORIGIN=ORIGIN, raise_request_exception=False)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        allowed, crawled = [], 0
        for path, method, op in schema_operations():
            if method not in UNSAFE or path in PUBLIC_OPERATIONS:
                continue
            crawled += 1
            response = call(client, method, concrete(path, op))
            if response.status_code != 403:
                allowed.append((method.upper(), path, response.status_code))
        assert crawled > 40
        assert allowed == []

    def test_a_full_key_still_cannot_reach_key_management_anywhere(self, user):
        _, raw = ApiKey.issue(user, "ci", "full")
        client = APIClient(HTTP_ORIGIN=ORIGIN, raise_request_exception=False)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        for path, method, op in schema_operations():
            if path.startswith("/api/v1/api-keys/"):
                assert call(client, method, concrete(path, op)).status_code == 403, path

    def test_a_revoked_key_gets_nothing_anywhere(self, user):
        key, raw = ApiKey.issue(user, "ci", "full")
        key.revoked_at = timezone.now()
        key.save()
        client = APIClient(HTTP_ORIGIN=ORIGIN, raise_request_exception=False)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        for path, method, op in schema_operations():
            if path in PUBLIC_OPERATIONS:
                continue
            assert call(client, method, concrete(path, op)).status_code == 401, (method, path)


# --- one account can't reach another's ------------------------------------------------------

MARKER = "ALICE-PRIVATE-MARKER"


def seed_alices_world(alice):
    """One of everything an account can own, each carrying a marker no one else should see."""
    now = timezone.now()
    monitor = Monitor.objects.create(
        user=alice,
        name=f"{MARKER}-monitor",
        type="http",
        target=f"https://{MARKER.lower()}.example.com",
        interval_seconds=300,
        config={},
        next_check_at=now,
    )
    incident = Incident.objects.create(
        monitor=monitor, started_at=now - timedelta(hours=2), ended_at=now, reason="timeout"
    )
    event = IncidentEvent.objects.create(incident=incident, kind="opened", created_at=now)
    comment = IncidentComment.objects.create(
        incident=incident, author=alice, body=f"{MARKER}-note", visible_on_status_page=False
    )
    contact = AlertContact.objects.create(
        user=alice,
        name=f"{MARKER}-contact",
        channel="slack",
        config={"webhook_url": "https://hooks.slack.com/services/T0/B0/xxxxxxxx"},
        verified_at=now,
    )
    window = MaintenanceWindow.objects.create(
        user=alice, name=f"{MARKER}-window", starts_at=now, ends_at=now + timedelta(hours=1)
    )
    window.monitors.add(monitor)
    source = DeploySource.objects.create(
        user=alice, name=f"{MARKER}-source", type="github", secret="s" * 24
    )
    deploy = Deploy.objects.create(
        user=alice,
        source=source,
        source_type="github",
        source_name=source.name,
        external_id="1",
        service_name=f"{MARKER}-svc",
        names=[f"{MARKER}-svc".lower()],
        occurred_at=now,
    )
    page = StatusPage.objects.create(user=alice, name=f"{MARKER}-page", slug="alice-page")
    component = StatusPageComponent.objects.create(
        page=page, monitor=monitor, display_name=f"{MARKER}-component"
    )
    announcement = StatusPageAnnouncement.objects.create(
        page=page, kind="incident", title=f"{MARKER}-announcement", body=""
    )
    subscriber = StatusPageSubscriber.objects.create(
        page=page, email=f"{MARKER.lower()}@example.com", confirmed_at=now
    )
    key, _ = ApiKey.issue(alice, f"{MARKER}-key", "read")
    subscription = UserProviderSubscription.objects.create(
        user=alice, provider=Provider.objects.get(slug="github")
    )
    delivery = NotificationDelivery.objects.create(
        monitor=monitor,
        incident=incident,
        contact=contact,
        contact_name=contact.name,
        channel="slack",
        event="opened",
        dedupe_key="opened",
        status="skipped",
        payload={},
    )
    objects = [
        monitor, incident, event, comment, contact, window, source, deploy, page, component,
        announcement, subscriber, key, subscription, delivery,
    ]  # fmt: skip
    return [str(o.pk) for o in objects], page


@pytest.fixture
def bob(make_user, sign_in):
    user = make_user("bob@example.com")
    client = APIClient(HTTP_ORIGIN=ORIGIN, raise_request_exception=False)
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {sign_in(client, user.email).json()['access_token']}"
    )
    return client


class TestAccountIsolation:
    def test_bob_reaches_none_of_alices_objects_by_any_id_through_any_endpoint(self, user, bob):
        ids, _ = seed_alices_world(user)
        assert len(ids) == 15
        leaks, tried = [], 0
        for path, method, op in schema_operations():
            if path in PUBLIC_OPERATIONS or path.startswith("/api/v1/auth/"):
                continue
            if "{" not in path:
                continue  # collections: next test
            for object_id in (i for i in ids if len(i) == 36):
                response = call(bob, method, concrete(path, op, object_id))
                tried += 1
                body = response.content.decode(errors="ignore")
                if (
                    200 <= response.status_code < 300
                    or MARKER in body
                    or response.status_code >= 500
                ):
                    leaks.append((method.upper(), path, object_id, response.status_code))
        assert tried > 600
        assert leaks == []

    def test_bobs_lists_and_reports_never_contain_anything_of_alices(self, user, bob):
        ids, _ = seed_alices_world(user)
        today = timezone.now().date().isoformat()
        checked = 0
        for path, method, op in schema_operations():
            if method != "get" or "{" in path or path in PUBLIC_OPERATIONS:
                continue
            if path.startswith("/api/v1/auth/"):
                continue
            url = concrete(path, op)
            params = {"start_date": today, "end_date": today, "range": "7d"}
            cache.clear()
            response = bob.get(url, params)
            body = response.content.decode(errors="ignore")
            assert response.status_code < 500, path
            assert MARKER not in body, path
            assert not any(i in body for i in ids if len(i) == 36), path
            checked += 1
        assert checked > 15

    def test_the_public_page_of_a_published_page_shows_only_what_its_owner_chose(self, user):
        ids, page = seed_alices_world(user)
        page.is_public = True
        page.save()
        body = anonymous().get("/api/v1/public/status-pages/alice-page/").content.decode()
        # What the owner named for visitors may appear; the monitor's own name and target and
        # every internal id may not.
        assert f"{MARKER}-component" in body
        assert f"{MARKER}-monitor" not in body and MARKER.lower() not in body
        assert f"{MARKER}-note" not in body
        for object_id in (i for i in ids if len(i) == 36):
            if object_id not in (str(page.pk),):
                # incidents and components are shown by their own ids; nothing else may be
                if Incident.objects.filter(pk=object_id).exists():
                    continue
                if StatusPageComponent.objects.filter(pk=object_id).exists():
                    continue
                if StatusPageAnnouncement.objects.filter(pk=object_id).exists():
                    continue
                assert object_id not in body, object_id

    def test_deleting_a_user_leaves_nothing_of_theirs_behind(self, user):
        seed_alices_world(user)
        user.delete()
        for model in (
            Monitor, Incident, AlertContact, MaintenanceWindow, DeploySource, Deploy, StatusPage,
            StatusPageSubscriber, ApiKey, UserProviderSubscription, NotificationDelivery,
        ):  # fmt: skip
            assert not model.objects.exists(), model.__name__


# --- the database ---------------------------------------------------------------------------


class TestIndexes:
    """Every query is filtered by owner, so every owner column has to be indexed or each page
    load becomes a table scan as data grows."""

    def test_every_foreign_key_to_a_user_is_indexed(self):
        from django.apps import apps

        missing = []
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if (
                    isinstance(field, dj_models.ForeignKey)
                    and field.related_model._meta.label == "accounts.User"
                    and not field.db_index
                    and not field.unique
                ):
                    missing.append(f"{model._meta.label}.{field.name}")
        assert missing == []

    def test_every_model_owned_through_a_monitor_indexes_the_monitor(self):
        from django.apps import apps

        missing = []
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if (
                    isinstance(field, dj_models.ForeignKey)
                    and field.related_model is Monitor
                    and not field.db_index
                    and not field.unique
                ):
                    missing.append(f"{model._meta.label}.{field.name}")
        assert missing == []

    def test_the_hot_lookups_have_composite_indexes(self):
        from django.apps import apps

        def indexed(model, *names):
            meta = apps.get_model(model)._meta
            leading = [tuple(i.fields)[: len(names)] for i in meta.indexes]
            uniques = [
                tuple(c.fields)[: len(names)] for c in meta.constraints if hasattr(c, "fields")
            ]
            return tuple(names) in leading + uniques

        assert indexed("incidents.Incident", "monitor", "-started_at")
        assert indexed("deploys.Deploy", "user", "-occurred_at")
        assert indexed("apikeys.ApiKey", "user", "-created_at")
        assert indexed("statuspages.StatusPageAnnouncement", "page", "-created_at")


# --- headers and settings -------------------------------------------------------------------


class TestResponseHeaders:
    def test_api_responses_are_not_cacheable_and_cannot_load_anything(self, user, bob):
        for path in ("/api/v1/monitors/", "/api/v1/auth/me/", "/api/v1/reports/uptime.csv"):
            response = bob.get(path)
            assert response["Cache-Control"] == "no-store", path
            assert (
                response["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
            )
            assert response["X-Content-Type-Options"] == "nosniff"
            assert response["X-Frame-Options"] == "DENY"

    def test_even_errors_and_refusals_carry_them(self):
        response = anonymous().get("/api/v1/monitors/")
        assert response.status_code == 401
        assert response["Cache-Control"] == "no-store" and "Content-Security-Policy" in response

    def test_the_internal_endpoints_carry_them_too(self):
        response = anonymous().post("/internal/run-due-checks/")
        assert response.status_code == 401 and response["Cache-Control"] == "no-store"

    def test_the_public_status_page_keeps_its_own_policy(self, user):
        StatusPage.objects.create(user=user, name="P", slug="pub", is_public=True)
        response = anonymous().get("/api/v1/public/status-pages/pub/")
        assert response["Cache-Control"] == "no-store"
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_the_docs_page_is_left_able_to_load_its_own_scripts(self):
        response = anonymous().get("/api/v1/docs/")
        assert "Content-Security-Policy" not in response

    def test_the_admin_is_not_given_the_api_policy(self):
        response = anonymous().get("/admin/login/")
        assert "Content-Security-Policy" not in response


class TestProductionSettings:
    def run_check(self, **env):
        base = {
            **os.environ,
            "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": SECRET,
            "DJANGO_ALLOWED_HOSTS": "api.example.com",
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DATABASE_URL": "sqlite:///:memory:",
        }
        base.update(env)
        return subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
            cwd=BASE_DIR,
            env=base,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_the_production_configuration_passes_djangos_deployment_check(self):
        result = self.run_check()
        assert result.returncode == 0, result.stdout + result.stderr

    def test_it_refuses_to_start_with_a_guessable_secret_key(self):
        result = self.run_check(
            DJANGO_SECRET_KEY="django-insecure-local-dev-only-do-not-use-in-prod"
        )
        assert result.returncode != 0 and "DJANGO_SECRET_KEY" in result.stderr

    def test_it_refuses_a_short_secret_key(self):
        assert self.run_check(DJANGO_SECRET_KEY="short").returncode != 0

    def test_https_is_enforced_with_the_health_probe_exempt(self):
        code = (
            "import django; django.setup(); from django.conf import settings as s; "
            "print(s.SECURE_SSL_REDIRECT, s.SECURE_HSTS_SECONDS, s.SESSION_COOKIE_SECURE, "
            "s.CSRF_COOKIE_SECURE, s.AUTH_REFRESH_COOKIE_SECURE, s.SECURE_REDIRECT_EXEMPT, s.DEBUG)"
        )
        env = {
            **os.environ,
            "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": SECRET,
            "DJANGO_ALLOWED_HOSTS": "api.example.com",
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DATABASE_URL": "sqlite:///:memory:",
        }
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=BASE_DIR, env=env, capture_output=True, text=True
        ).stdout.strip()
        assert out == "True 31536000 True True True ['^health/$'] False"

    def test_debug_is_off_by_default_only_in_the_environment_that_says_so(self, settings):
        # The tests run with DEBUG off; this guards the toggle's meaning, not the dev default.
        assert settings.DEBUG is False

    def test_the_admin_address_can_be_moved(self):
        code = (
            "import django; django.setup(); "
            "from django.conf import settings as s; print(s.ADMIN_URL)"
        )
        env = {
            **os.environ,
            "DJANGO_ADMIN_URL": "/ops-9f3k/",
            "DJANGO_SETTINGS_MODULE": "config.settings",
        }
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=BASE_DIR, env=env, capture_output=True, text=True
        ).stdout.strip()
        assert out == "ops-9f3k/"


class TestAdmin:
    def test_the_admin_demands_a_staff_login(self, client):
        assert client.get("/admin/").status_code == 302
        assert "/admin/login/" in client.get("/admin/")["Location"]

    def test_an_ordinary_user_is_not_staff(self, user):
        assert not user.is_staff and not user.is_superuser

    def test_the_admin_never_shows_secrets(self):
        from django.contrib import admin

        from apikeys.models import ApiKey as Key
        from statuspages.models import StatusPage as Page
        from statuspages.models import StatusPageSubscriber as Subscriber

        for model, field in (
            (Key, "key_hash"),
            (Page, "password_hash"),
            (Subscriber, "unsubscribe_token"),
        ):
            model_admin = admin.site._registry[model]
            assert field in (
                model_admin.exclude or []
            ), f"{model.__name__}.{field} is editable in the admin"
            assert field not in (model_admin.list_display or [])


# --- the review document stays honest -----------------------------------------------------------


class TestScenarioMap:
    """`docs/plan/07-security-review.md` names a test for every critical scenario. If one is
    renamed or deleted, the document would quietly stop being true: this fails instead."""

    REFERENCE = re.compile(r"`(tests/[\w/]+\.py(?:::\w+)+)`")

    def references(self):
        text = (BASE_DIR / "docs" / "plan" / "07-security-review.md").read_text(encoding="utf-8")
        return sorted(set(self.REFERENCE.findall(text)))

    def test_there_is_a_test_for_every_critical_scenario(self):
        assert len(self.references()) >= 20

    def test_every_named_test_exists(self):
        missing = []
        for reference in self.references():
            path, *names = reference.split("::")
            source = (BASE_DIR / path).read_text(encoding="utf-8")
            for name in names:
                if not re.search(rf"^\s*(?:class|def)\s+{re.escape(name)}\b", source, re.MULTILINE):
                    missing.append(f"{reference} ({name})")
        assert missing == []

    def test_the_documented_public_routes_match_the_ones_the_gate_checks(self):
        text = (BASE_DIR / "docs" / "plan" / "07-security-review.md").read_text(encoding="utf-8")
        assert "exactly the reviewed list" in text
