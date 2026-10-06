"""The scheduler trigger runs checks, dependency reads, alerts and subscriber mail in one HTTP
call. Its caller gives up after 150 s, so the dependency reads (the only optional part) get
whatever time the checks left, and alerts always run."""

import time

import pytest
from rest_framework.test import APIClient

from monitoring import internal_views, signing
from monitoring.engine import PassSummary

pytestmark = pytest.mark.django_db

SECRET = "test-scheduler-secret"


def call():
    return APIClient().post(
        "/internal/run-due-checks/",
        HTTP_X_ROOTPULSE_SIGNATURE=signing.sign(SECRET, b""),
    )


@pytest.fixture
def seen(monkeypatch):
    """Record the time budget the dependency reads were given, and that alerts and mail ran."""
    calls = {"provider_budget": None, "notifications": 0, "mail": 0}

    from notifications import dispatcher
    from providers import poller
    from providers.poller import PollSummary
    from statuspages import mail

    real_notifications = dispatcher.run_notifications_once
    real_mail = mail.run_status_page_mail_once

    def fake_polls(*, time_budget=None, **_):
        calls["provider_budget"] = time_budget
        return PollSummary()

    def fake_notifications(**_):
        calls["notifications"] += 1
        return real_notifications()

    def fake_mail(**_):
        calls["mail"] += 1
        return real_mail()

    monkeypatch.setattr(poller, "run_provider_polls_once", fake_polls)
    monkeypatch.setattr(dispatcher, "run_notifications_once", fake_notifications)
    monkeypatch.setattr(mail, "run_status_page_mail_once", fake_mail)
    return calls


def test_a_quick_pass_leaves_the_dependency_reads_their_usual_time(seen, monkeypatch):
    monkeypatch.setattr(internal_views, "run_scheduler_once", lambda: PassSummary())
    assert call().status_code == 200
    assert seen["provider_budget"] == pytest.approx(40, abs=1)


def test_a_slow_pass_shrinks_the_dependency_reads_but_not_alerts(seen, settings, monkeypatch):
    settings.TRIGGER_BUDGET_SECONDS = 110

    def slow_pass():
        # Pretend the checks took 90 of the 110 seconds.
        real = time.monotonic
        monkeypatch.setattr(internal_views.time, "monotonic", lambda: real() + 90)
        return PassSummary()

    monkeypatch.setattr(internal_views, "run_scheduler_once", slow_pass)
    assert call().status_code == 200
    assert seen["provider_budget"] == 1.0  # nothing to spare, but never zero or negative
    assert seen["notifications"] == 1  # alerts are not optional
    assert seen["mail"] == 1


def test_the_dependency_reads_never_use_more_than_their_own_cap(seen, settings, monkeypatch):
    settings.TRIGGER_BUDGET_SECONDS = 1000
    monkeypatch.setattr(internal_views, "run_scheduler_once", lambda: PassSummary())
    call()
    assert seen["provider_budget"] <= settings.PROVIDER_PASS_BUDGET_SECONDS


def test_the_response_still_carries_every_part_of_the_summary(seen, monkeypatch):
    monkeypatch.setattr(internal_views, "run_scheduler_once", lambda: PassSummary())
    body = call().json()
    assert {"claimed", "providers", "notifications", "status_page_mail"} <= set(body)
