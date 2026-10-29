import re

from cf_email_unwrapper.queue_name import queue_name


def test_contract_vectors():
    assert queue_name("hermes@example.org") == "mail-hermes-example-org"
    assert queue_name("  Hermes.Bot+x@Example.ORG ") == "mail-hermes-bot-x-example-org"


def test_long_address_is_truncated_with_hash():
    name = queue_name("a" * 70 + "@example.org")
    assert len(name) <= 63
    assert re.fullmatch(r"mail-a+-[0-9a-f]{8}", name)
    # Same value as cf-email-gateway computes for this address.
    assert name == "mail-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-0ffad590"


def test_truncation_does_not_leave_double_dash():
    # Position 54 falls right after a separator run.
    address = "a" * 48 + ".b" + "c" * 30 + "@x.io"
    name = queue_name(address)
    assert "--" not in name and len(name) <= 63
