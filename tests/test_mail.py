import smtplib
import socket

import pytest
from django.core.mail.backends.smtp import EmailBackend

from accounts import mail


def make_backend():
    return mail.RetryingSMTPBackend(host="smtp.test", port=587, timeout=1)


def test_retries_a_stalled_connection_then_succeeds(monkeypatch):
    calls = []

    def flaky_open(self):
        calls.append(1)
        if len(calls) < mail.ATTEMPTS:
            raise socket.timeout("timed out")
        return True

    monkeypatch.setattr(EmailBackend, "open", flaky_open)
    assert make_backend().open() is True
    assert len(calls) == mail.ATTEMPTS


def test_gives_up_after_the_last_attempt(monkeypatch):
    calls = []

    def dead_open(self):
        calls.append(1)
        raise smtplib.SMTPServerDisconnected("closed")

    monkeypatch.setattr(EmailBackend, "open", dead_open)
    with pytest.raises(smtplib.SMTPServerDisconnected):
        make_backend().open()
    assert len(calls) == mail.ATTEMPTS


def test_does_not_retry_bad_credentials(monkeypatch):
    calls = []

    def bad_login(self):
        calls.append(1)
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(EmailBackend, "open", bad_login)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        make_backend().open()
    assert len(calls) == 1
