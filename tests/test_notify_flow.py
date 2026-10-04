"""When alerts are created, sent, retried, skipped and cancelled: the rules of Phase 8.

Channel adapters are replaced by a recorder (their own behaviour is in test_notify_channels),
so these tests are about *who* hears about *what*, and *when*."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from incidents import services as incident_services
from incidents.models import Incident, IncidentEvent
from monitoring.checks import result as r
from monitoring.engine import Engine, run_scheduler_once
from monitoring.models import AlertContact, Check, MaintenanceWindow, Monitor
from notifications import dispatcher
from notifications import services as notify
from notifications.channels import CHANNELS
from notifications.http import DeliveryError
from notifications.models import NotificationDelivery as Delivery

pytestmark = pytest.mark.django_db

UP = r.CheckResult(success=True, status_detail=r.OK, status_code=200, duration_ms=120)
DOWN = r.CheckResult(
    success=False, status_detail=r.STATUS_MISMATCH, status_code=503, duration_ms=80
)

CONFIGS = {
    "email": {"address": "alice@example.com"},
    "slack": {"webhook_url": "https://hooks.slack.com/services/T000/B000/abcdefghijkl"},
    "webhook": {"url": "https://hooks.example.com/in", "secret": "s"},
    "discord": {"webhook_url": "https://discord.com/api/webhooks/123456789/abcDEF"},
    "telegram": {"bot_token": "123456789:" + "A" * 35, "chat_id": "5"},
}


@pytest.fixture
def outbox(monkeypatch):
    """Replaces every channel's `send`. `outbox.sent` records what went out;
    `outbox.fail[channel]` = an exception (or a list of them, used in order) to raise."""

    class Outbox:
        def __init__(self):
            self.sent = []
            self.fail = {}

    box = Outbox()

    def install(key):
        def send(config, payload, delivery_id):
            problem = box.fail.get(key)
            if isinstance(problem, list):
                problem = problem.pop(0) if problem else None
            if problem:
                raise problem
            box.sent.append((key, payload["event"], payload["title"], delivery_id))

        monkeypatch.setattr(CHANNELS[key], "send", send)

    for key in CHANNELS:
        install(key)
    return box


def monitor_for(user, name="Shop", **kwargs):
    fields = {
        "type": "http",
        "target": "https://shop.example.com",
        "interval_seconds": 300,
        "config": {},
        "status": "up",
        "next_check_at": timezone.now() - timedelta(minutes=1),
    }
    fields.update(kwargs)
    return Monitor.objects.create(user=user, name=name, **fields)


def contact_for(user, monitor=None, channel="slack", **kwargs):
    fields = {"name": f"my {channel}", "channel": channel, "config": CONFIGS[channel]}
    if channel == "email":
        fields["verified_at"] = timezone.now()
    fields.update(kwargs)
    contact = AlertContact.objects.create(user=user, **fields)
    if monitor is not None:
        monitor.alert_contacts.add(contact)
    return contact


def script(*outcomes):
    calls = []

    def runner(monitor):
        calls.append(1)
        return (outcomes[min(len(calls) - 1, len(outcomes) - 1)], None)

    return runner


def run_checks(runner):
    Monitor.objects.update(next_check_at=timezone.now() - timedelta(minutes=1))
    return run_scheduler_once(engine=Engine(runner=runner, sleep=lambda s: None))


def fail_it(monitor):
    run_checks(script(DOWN, DOWN))
    return Incident.objects.get(monitor=monitor, ended_at__isnull=True)


def recover(monitor):
    monitor.refresh_from_db()
    run_checks(script(UP))


def send_due(at=None):
    return dispatcher.run_notifications_once(now=at or timezone.now(), workers=1)


def deliveries(**filters):
    return list(Delivery.objects.filter(**filters).order_by("id"))


# --- the definition of done -----------------------------------------------------------------


class TestOpening:
    def test_an_incident_on_a_monitor_with_three_channels_produces_three_deliveries(
        self, user, outbox
    ):
        monitor = monitor_for(user)
        contact_for(user, monitor, "email")
        contact_for(user, monitor, "slack")
        contact_for(user, monitor, "webhook")

        incident = fail_it(monitor)
        assert [d.status for d in deliveries(incident=incident)] == ["pending"] * 3

        summary = send_due()
        assert summary.sent == 3
        assert sorted(c for c, *_ in outbox.sent) == ["email", "slack", "webhook"]
        assert all(d.status == "sent" and d.sent_at for d in deliveries(incident=incident))
        assert {t for *_, t, _ in outbox.sent} == {"Shop is DOWN"}

    def test_each_send_lands_on_the_incident_timeline(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", name="Ops room")
        incident = fail_it(monitor)
        send_due()

        event = IncidentEvent.objects.get(incident=incident, kind="notification_sent")
        assert event.metadata["contact"] == "Ops room"
        assert event.metadata["channel"] == "slack"

    def test_a_failure_in_maintenance_alerts_nobody(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        contact_for(user, monitor, "email")
        now = timezone.now()
        window = MaintenanceWindow.objects.create(
            user=user,
            name="Upgrade",
            starts_at=now - timedelta(minutes=5),
            ends_at=now + timedelta(hours=1),
        )
        window.monitors.add(monitor)

        check = Check.objects.create(
            monitor=monitor, region="primary", started_at=now, success=False,
            status_detail="timeout", duration_ms=10000,
        )  # fmt: skip
        incident_services.open_incident(monitor, check)

        rows = deliveries()
        assert len(rows) == 2 and all(d.status == "skipped" for d in rows)
        assert all("maintenance" in d.error for d in rows)
        assert send_due().claimed == 0 and outbox.sent == []

    def test_a_window_that_starts_before_the_alert_goes_out_still_suppresses_it(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", delay_seconds=600)
        fail_it(monitor)

        now = timezone.now()
        window = MaintenanceWindow.objects.create(
            user=user,
            name="Late",
            starts_at=now + timedelta(minutes=1),
            ends_at=now + timedelta(hours=2),
        )
        window.monitors.add(monitor)

        send_due(now + timedelta(minutes=11))
        assert outbox.sent == []
        assert deliveries()[0].status == "skipped"

    def test_other_users_and_unattached_contacts_hear_nothing(self, user, make_user, outbox):
        monitor = monitor_for(user)
        attached = contact_for(user, monitor, "slack")
        contact_for(user, None, "discord")  # not attached
        stranger = make_user("bob@example.com")
        contact_for(stranger, None, "telegram")

        fail_it(monitor)
        send_due()
        assert [c for c, *_ in outbox.sent] == ["slack"]
        assert deliveries()[0].contact == attached

    def test_disabled_and_unverified_contacts_are_left_out(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", enabled=False)
        contact_for(user, monitor, "email", verified_at=None)
        ok = contact_for(user, monitor, "discord")

        fail_it(monitor)
        assert [d.contact for d in deliveries()] == [ok]

    def test_scheduling_twice_never_alerts_twice(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        incident = fail_it(monitor)

        assert notify.schedule_opened(incident) == []
        assert len(deliveries()) == 1
        send_due()
        send_due()
        assert len(outbox.sent) == 1


class TestDelay:
    def test_waits_for_the_contacts_delay(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", delay_seconds=300)
        fail_it(monitor)
        now = timezone.now()

        assert send_due(now + timedelta(seconds=120)).claimed == 0
        assert outbox.sent == []
        assert send_due(now + timedelta(seconds=310)).sent == 1

    def test_recovering_inside_the_delay_sends_nothing_at_all(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", delay_seconds=300)
        fail_it(monitor)
        recover(monitor)

        send_due(timezone.now() + timedelta(hours=1))
        assert outbox.sent == []  # no "down" and, crucially, no "back up"
        assert [(d.event, d.status) for d in deliveries()] == [("opened", "skipped")]
        assert "recovered" in deliveries()[0].error

    def test_different_delays_per_contact(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        contact_for(user, monitor, "discord", delay_seconds=600)
        fail_it(monitor)
        now = timezone.now()

        send_due(now)
        assert [c for c, *_ in outbox.sent] == ["slack"]
        send_due(now + timedelta(minutes=11))
        assert [c for c, *_ in outbox.sent] == ["slack", "discord"]


class TestRecovery:
    def test_contacts_that_were_told_are_told_it_is_fixed(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        fail_it(monitor)
        send_due()
        recover(monitor)
        send_due()

        assert [(c, e) for c, e, *_ in outbox.sent] == [("slack", "opened"), ("slack", "resolved")]
        assert outbox.sent[1][2] == "Shop is back UP"

    def test_only_those_who_heard_about_the_outage_hear_about_the_recovery(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        contact_for(user, monitor, "discord", delay_seconds=3600)
        fail_it(monitor)
        send_due()  # slack is told; discord is still waiting
        recover(monitor)
        send_due(timezone.now() + timedelta(hours=2))

        assert [(c, e) for c, e, *_ in outbox.sent] == [("slack", "opened"), ("slack", "resolved")]

    def test_no_all_clear_for_an_alert_that_failed_permanently(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        outbox.fail["slack"] = DeliveryError("gone", retryable=False)
        fail_it(monitor)
        send_due()
        recover(monitor)
        send_due()
        assert outbox.sent == []
        assert [d.event for d in deliveries()] == ["opened"]


class TestRetries:
    def test_a_temporary_failure_is_retried_with_backoff_then_succeeds(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        outbox.fail["slack"] = [DeliveryError("busy", retryable=True)]
        fail_it(monitor)
        now = timezone.now()

        first = send_due(now)
        delivery = deliveries()[0]
        assert (first.retrying, delivery.status, delivery.attempt_count) == (1, "pending", 1)
        assert delivery.error == "busy"
        assert (
            timedelta(seconds=50)
            < delivery.next_attempt_at - timezone.now()
            <= timedelta(seconds=60)
        )

        assert send_due(now + timedelta(seconds=30)).claimed == 0  # not due yet
        assert send_due(now + timedelta(seconds=90)).sent == 1
        delivery.refresh_from_db()
        assert (delivery.status, delivery.attempt_count, delivery.error) == ("sent", 2, "")

    def test_gives_up_after_the_maximum_attempts_and_says_so_on_the_timeline(
        self, user, outbox, settings
    ):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", name="Ops room")
        outbox.fail["slack"] = DeliveryError("still busy", retryable=True)
        incident = fail_it(monitor)

        at = timezone.now()
        for _ in range(settings.NOTIFY_MAX_ATTEMPTS):
            send_due(at)
            at += timedelta(hours=1)

        delivery = deliveries()[0]
        assert (delivery.status, delivery.attempt_count) == ("failed", settings.NOTIFY_MAX_ATTEMPTS)
        assert delivery.next_attempt_at is None
        failure = IncidentEvent.objects.get(incident=incident, kind="notification_failed")
        assert (
            failure.metadata["contact"] == "Ops room" and failure.metadata["error"] == "still busy"
        )
        assert send_due(at).claimed == 0  # and it stays given up on

    def test_backoff_grows(self, user, outbox, settings):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        outbox.fail["slack"] = DeliveryError("busy", retryable=True)
        fail_it(monitor)

        gaps, at = [], timezone.now()
        for _ in range(3):
            send_due(at)
            gaps.append(
                round((deliveries()[0].next_attempt_at - timezone.now()).total_seconds(), -1)
            )
            at += timedelta(hours=1)
        assert gaps == [60, 300, 900]

    def test_a_permanent_failure_is_not_retried(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        outbox.fail["slack"] = DeliveryError("The webhook is gone", retryable=False)
        fail_it(monitor)

        summary = send_due()
        delivery = deliveries()[0]
        assert (summary.failed, delivery.status, delivery.attempt_count) == (1, "failed", 1)

    def test_the_servers_retry_after_is_respected(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        outbox.fail["slack"] = DeliveryError("slow down", retryable=True, retry_after=240)
        fail_it(monitor)
        send_due()
        wait = (deliveries()[0].next_attempt_at - timezone.now()).total_seconds()
        assert 230 < wait <= 240

    def test_an_expired_push_subscription_switches_the_contact_off(self, user, outbox):
        monitor = monitor_for(user)
        contact = contact_for(user, monitor, "slack")
        gone = DeliveryError("expired", retryable=False)
        gone.disable_contact = True
        outbox.fail["slack"] = gone
        fail_it(monitor)
        send_due()
        contact.refresh_from_db()
        assert contact.enabled is False

    def test_a_crashing_adapter_does_not_lose_the_alert(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        outbox.fail["slack"] = [RuntimeError("bug")]
        fail_it(monitor)
        send_due()
        delivery = deliveries()[0]
        assert delivery.status == "pending" and "Unexpected" in delivery.error
        assert send_due(timezone.now() + timedelta(hours=1)).sent == 1

    def test_one_contacts_failure_does_not_block_the_others(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        contact_for(user, monitor, "discord")
        outbox.fail["slack"] = DeliveryError("nope", retryable=False)
        fail_it(monitor)
        summary = send_due()
        assert (summary.sent, summary.failed) == (1, 1)
        assert [c for c, *_ in outbox.sent] == ["discord"]

    def test_a_claimed_delivery_is_not_claimed_again_while_leased(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        fail_it(monitor)
        now = timezone.now()
        assert len(dispatcher.claim_due(now, 10)) == 1
        assert dispatcher.claim_due(now, 10) == []
        # A crashed worker's claim expires, and the alert goes out on a later pass.
        assert (
            len(dispatcher.claim_due(now + dispatcher.CLAIM_LEASE + timedelta(seconds=1), 10)) == 1
        )

    def test_the_batch_size_limits_a_pass(self, user, outbox):
        monitor = monitor_for(user)
        for channel in ("slack", "discord", "telegram"):
            contact_for(user, monitor, channel)
        fail_it(monitor)
        first = dispatcher.run_notifications_once(batch_size=2, workers=1)
        assert first.claimed == 2
        assert dispatcher.run_notifications_once(batch_size=2, workers=1).claimed == 1

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.skipif(connection.vendor != "postgresql", reason="worker threads need Postgres")
    def test_the_worker_pool_sends_everything_once(self, user, outbox):
        monitor = monitor_for(user)
        for channel in ("slack", "discord", "telegram", "webhook"):
            contact_for(user, monitor, channel)
        fail_it(monitor)
        summary = dispatcher.run_notifications_once(workers=4)
        assert summary.sent == 4 and len(outbox.sent) == 4


class TestCancelling:
    def test_pausing_cancels_alerts_still_waiting(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", delay_seconds=600)
        fail_it(monitor)
        incident_services.close_for_pause(monitor)
        send_due(timezone.now() + timedelta(hours=1))
        assert outbox.sent == []
        assert "paused" in deliveries()[0].error and deliveries()[0].status == "skipped"

    def test_editing_cancels_alerts_still_waiting(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", delay_seconds=600)
        fail_it(monitor)
        incident_services.close_for_edit(monitor)
        send_due(timezone.now() + timedelta(hours=1))
        assert outbox.sent == [] and deliveries()[0].status == "skipped"

    def test_deleting_a_contact_keeps_history_and_sends_nothing_more(self, user, outbox):
        monitor = monitor_for(user)
        contact = contact_for(user, monitor, "slack", name="Old room", delay_seconds=600)
        fail_it(monitor)
        contact.delete()
        send_due(timezone.now() + timedelta(hours=1))
        delivery = deliveries()[0]
        assert outbox.sent == []
        assert (delivery.contact, delivery.contact_name, delivery.status) == (
            None,
            "Old room",
            "skipped",
        )

    def test_switching_a_contact_off_stops_alerts_already_queued(self, user, outbox):
        monitor = monitor_for(user)
        contact = contact_for(user, monitor, "slack", delay_seconds=600)
        fail_it(monitor)
        AlertContact.objects.filter(pk=contact.pk).update(enabled=False)
        send_due(timezone.now() + timedelta(hours=1))
        assert outbox.sent == []


class TestReminders:
    def setup_down(self, user, outbox, **kwargs):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", repeat_interval_seconds=300, **kwargs)
        incident = fail_it(monitor)
        now = timezone.now()
        send_due(now)
        return monitor, incident, now

    def test_reminds_on_the_contacts_cadence(self, user, outbox):
        _, _, now = self.setup_down(user, outbox)
        send_due(now + timedelta(minutes=2))
        assert [e for _, e, *_ in outbox.sent] == ["opened"]  # too soon

        send_due(now + timedelta(minutes=6))
        assert [e for _, e, *_ in outbox.sent] == ["opened", "reminder"]
        send_due(now + timedelta(minutes=8))
        assert [e for _, e, *_ in outbox.sent] == [
            "opened",
            "reminder",
        ]  # clock restarts at each send
        send_due(now + timedelta(minutes=12))
        assert [e for _, e, *_ in outbox.sent] == ["opened", "reminder", "reminder"]
        assert outbox.sent[1][2] == "Shop is still DOWN"

    def test_stops_at_the_cap(self, user, outbox, settings):
        settings.NOTIFY_MAX_REMINDERS = 2
        _, _, now = self.setup_down(user, outbox)
        for n in range(1, 8):
            send_due(now + timedelta(minutes=6 * n))
        assert [e for _, e, *_ in outbox.sent].count("reminder") == 2

    def test_no_reminders_without_a_repeat_interval(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")  # repeat 0
        fail_it(monitor)
        now = timezone.now()
        send_due(now)
        send_due(now + timedelta(days=2))
        assert [e for _, e, *_ in outbox.sent] == ["opened"]

    def test_no_reminders_after_recovery(self, user, outbox):
        monitor, _, now = self.setup_down(user, outbox)
        recover(monitor)
        send_due(now + timedelta(hours=3))
        assert [e for _, e, *_ in outbox.sent] == ["opened", "resolved"]

    def test_no_reminders_during_maintenance(self, user, outbox):
        monitor, _, now = self.setup_down(user, outbox)
        window = MaintenanceWindow.objects.create(
            user=user,
            name="m",
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(days=1),
        )
        window.monitors.add(monitor)
        send_due(now + timedelta(hours=1))
        assert [e for _, e, *_ in outbox.sent] == ["opened"]

    def test_a_contact_never_told_about_the_outage_gets_no_reminder(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack", repeat_interval_seconds=300)
        outbox.fail["slack"] = DeliveryError("nope", retryable=False)
        fail_it(monitor)
        now = timezone.now()
        send_due(now)
        outbox.fail.clear()
        send_due(now + timedelta(hours=1))
        assert outbox.sent == []


class TestTestAlerts:
    def test_a_test_alert_is_not_an_incident_event(self, user, outbox):
        contact = contact_for(user, None, "slack")
        delivery = notify.create_test(contact)
        assert (delivery.event, delivery.incident, delivery.monitor) == ("test", None, None)
        assert dispatcher.deliver(delivery.id) == "sent"
        assert not IncidentEvent.objects.filter(kind="notification_sent").exists()
        assert outbox.sent[0][1] == "test"

    def test_a_test_for_a_monitor_names_it(self, user, outbox):
        monitor = monitor_for(user, name="Checkout")
        contact = contact_for(user, monitor, "slack")
        delivery = notify.create_test(contact, monitor)
        dispatcher.deliver(delivery.id)
        assert outbox.sent[0][2] == "Test alert for Checkout"


class TestCommands:
    def test_run_notifications_once_reports_a_summary(self, user, outbox):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        fail_it(monitor)
        out = StringIO()
        call_command("run_notifications_once", stdout=out)
        assert "sent=1" in out.getvalue()

    def test_run_engine_checks_then_alerts(self, user, outbox, monkeypatch):
        monitor = monitor_for(user)
        contact_for(user, monitor, "slack")
        from monitoring.checks.http_check import HttpChecker

        monkeypatch.setattr(HttpChecker, "run", lambda self, *a, **k: DOWN, raising=False)
        out = StringIO()
        call_command("run_engine", stdout=out)
        assert "checks:" in out.getvalue() and "alerts:" in out.getvalue()
