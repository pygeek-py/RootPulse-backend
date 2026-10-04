"""Extension points the engine calls when something noteworthy happens.

Phase 7 (incidents) and Phase 8 (notifications) hook in here, so the engine
itself never needs to know about them.
"""

from __future__ import annotations

import logging

from .models import Check, Monitor

logger = logging.getLogger(__name__)


def status_changed(monitor: Monitor, old: str, new: str, check: Check) -> None:
    """Called after a monitor's status changed because of `check` (already saved).

    A transition into "down" only happens once the failure has been confirmed.
    """
    logger.info(
        "monitor %s (%s): %s -> %s (%s)", monitor.id, monitor.name, old, new, check.status_detail
    )


def reconcile_incident(monitor: Monitor, new_status: str, check: Check, confirmations=()) -> None:
    """Keep the monitor's incident in step with its status (see incidents/services.py)."""
    from incidents import services  # imported here: incidents depends on monitoring, not vice versa

    services.reconcile(monitor, new_status, check, confirmations)
