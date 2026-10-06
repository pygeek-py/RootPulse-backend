"""Live tests talk to the real internet, so the autouse stand-ins the rest of the suite relies on
(a DNS that answers 93.184.216.34 for everything, a provider client that refuses connections)
are switched off here."""

import pytest


@pytest.fixture(autouse=True)
def dns():
    """Real DNS: the same fixture name the rest of the suite uses, doing nothing."""
    return {}


@pytest.fixture(autouse=True)
def no_provider_network():
    """Real provider status pages."""
    return None
