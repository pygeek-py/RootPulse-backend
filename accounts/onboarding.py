"""The setup checklist: which of the first steps a person has done.

Nothing about progress is stored. Each step is a question about the person's own data ("do they
have a monitor?"), so the list can't drift from reality: delete your only monitor and the step
is open again, connect a deploy source from anywhere and it ticks itself. What is remembered is
only when everything was first done (so it can be celebrated once) and whether they dismissed the
card.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from .models import User

# Order is the order they're shown in. The first four are the roadmap's; dependencies is the
# fifth because knowing whether a problem is yours or a provider's is part of what RootPulse is for.
STEP_IDS = ("monitor", "alerts", "deploys", "dependencies", "status-page")


def _done(user: User) -> dict[str, bool]:
    # Imported here: these apps depend on accounts, not the other way round.
    from deploys.models import DeploySource
    from monitoring.models import AlertContact, Monitor
    from notifications.services import deliverable
    from providers.models import UserProviderSubscription
    from statuspages.models import StatusPage

    return {
        "monitor": Monitor.objects.filter(user=user).exists(),
        # A channel that can actually reach them: an unconfirmed email or a switched-off contact
        # isn't "set up".
        "alerts": any(deliverable(c) for c in AlertContact.objects.filter(user=user)),
        "deploys": DeploySource.objects.filter(user=user).exists(),
        "dependencies": UserProviderSubscription.objects.filter(user=user).exists(),
        "status-page": StatusPage.objects.filter(user=user).exists(),
    }


def progress(user: User, *, now: datetime | None = None) -> dict:
    done = _done(user)
    steps = [{"id": step, "done": done[step]} for step in STEP_IDS]
    complete = all(done.values())
    if complete and user.onboarding_completed_at is None:
        user.onboarding_completed_at = now or timezone.now()
        user.save(update_fields=["onboarding_completed_at"])
    return {
        "steps": steps,
        "done_count": sum(done.values()),
        "total": len(steps),
        "completed_at": user.onboarding_completed_at,
        "dismissed": user.onboarding_dismissed_at is not None,
    }


def set_dismissed(user: User, dismissed: bool, *, now: datetime | None = None) -> None:
    user.onboarding_dismissed_at = (now or timezone.now()) if dismissed else None
    user.save(update_fields=["onboarding_dismissed_at"])
