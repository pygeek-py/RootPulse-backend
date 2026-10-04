import pytest

from monitoring import signing

SECRET = "s3cret"
NOW = 1_700_000_000


def test_a_fresh_correct_signature_verifies():
    header = signing.sign(SECRET, b'{"a":1}', now=NOW)
    assert signing.verify(SECRET, header, b'{"a":1}', now=NOW + 10)


def test_an_empty_body_works_for_the_scheduler_trigger():
    assert signing.verify(SECRET, signing.sign(SECRET, now=NOW), b"", now=NOW)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.replace("v1=", "v1=0"),  # wrong MAC
        lambda h: "garbage",
        lambda h: "",
        lambda h: "t=abc,v1=ff",
        lambda h: h.split(",")[0],  # missing v1
    ],
)
def test_malformed_or_forged_headers_are_refused(mutate):
    header = mutate(signing.sign(SECRET, b"x", now=NOW))
    assert not signing.verify(SECRET, header, b"x", now=NOW)


def test_a_tampered_body_is_refused():
    header = signing.sign(SECRET, b"original", now=NOW)
    assert not signing.verify(SECRET, header, b"tampered", now=NOW)


def test_the_wrong_secret_is_refused():
    assert not signing.verify("other", signing.sign(SECRET, b"x", now=NOW), b"x", now=NOW)


def test_old_and_future_timestamps_are_refused_so_captures_cannot_be_replayed():
    header = signing.sign(SECRET, b"x", now=NOW)
    assert not signing.verify(SECRET, header, b"x", now=NOW + 301)
    assert not signing.verify(SECRET, header, b"x", now=NOW - 301)
    assert signing.verify(SECRET, header, b"x", now=NOW + 299)


def test_a_missing_header_or_secret_never_verifies():
    assert not signing.verify(SECRET, None, b"", now=NOW)
    assert not signing.verify("", signing.sign("", b"", now=NOW), b"", now=NOW)


def test_the_timestamp_is_covered_by_the_mac():
    header = signing.sign(SECRET, b"x", now=NOW)
    forged = header.replace(f"t={NOW}", f"t={NOW + 5}")
    assert not signing.verify(SECRET, forged, b"x", now=NOW)
