"""SMTP backend that retries a stalled connection.

Some networks occasionally accept the TCP connection but never deliver the
server's greeting (seen intermittently with smtp.gmail.com), which would
otherwise hang for the whole timeout and then fail the sign-in email. A healthy
greeting arrives in well under a second, so a short timeout plus a couple of
retries is both faster and far more reliable. Authentication errors are not
retried: those won't fix themselves.
"""

from __future__ import annotations

import logging
import smtplib

from django.core.mail.backends.smtp import EmailBackend

logger = logging.getLogger(__name__)

ATTEMPTS = 4


class RetryingSMTPBackend(EmailBackend):
    def open(self) -> bool:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return super().open()
            except smtplib.SMTPAuthenticationError:
                raise  # (an OSError subclass, so it must be excluded before the catch below)
            except OSError:
                self.close()
                if attempt == ATTEMPTS:
                    raise
                logger.warning("SMTP connection attempt %s/%s failed; retrying", attempt, ATTEMPTS)
        return False  # pragma: no cover
