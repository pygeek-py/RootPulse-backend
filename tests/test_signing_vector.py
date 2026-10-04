import json
from pathlib import Path

from monitoring import signing

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "signing_vector.json").read_text())


def test_python_matches_the_shared_vector():
    """The same vector is checked by the Worker's JS tests, so the two signers can't drift."""
    header = signing.sign(VECTOR["secret"], VECTOR["body"].encode(), now=VECTOR["timestamp"])
    assert header == VECTOR["header"]
    assert signing.verify(
        VECTOR["secret"], VECTOR["header"], VECTOR["body"].encode(), now=VECTOR["timestamp"]
    )
