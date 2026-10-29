from datetime import UTC, datetime, timedelta

import pytest
from conftest import AUTHOR, OPENSSL, mail_headers

from cf_email_unwrapper.mail import extract_text
from cf_email_unwrapper.smime import Rejected, Smime

ALLOWED = frozenset({AUTHOR})
HOUR = timedelta(hours=1)


def open_mail(pki, raw, *, allowed=ALLOWED, now=None):
    return pki.smime().open(raw, allowed_senders=allowed, max_age=HOUR, now=now or datetime.now(UTC))


def test_encrypted_and_signed_mail_is_opened(pki):
    raw = pki.seal("Hi there", headers=mail_headers(), signer=pki.author_sign,
                   extra_certs=[pki.author_enc])
    opened = open_mail(pki, raw)
    assert opened.sender == AUTHOR
    assert opened.encrypted
    assert extract_text(opened.content) == "Hi there"
    assert opened.reply_cert_pem is not None


def test_signed_unencrypted_mail_is_accepted(pki):
    raw = pki.seal("Plain but signed", headers=mail_headers(), signer=pki.author_sign, encrypt=False)
    opened = open_mail(pki, raw)
    assert not opened.encrypted
    assert extract_text(opened.content) == "Plain but signed"
    assert opened.reply_cert_pem is None


@pytest.mark.parametrize("encrypt", [False, True])
def test_unsigned_mail_is_rejected(pki, encrypt):
    raw = pki.seal("Unsigned", headers=mail_headers(), signer=None, encrypt=encrypt)
    with pytest.raises(Rejected, match="not signed"):
        open_mail(pki, raw)


def test_signature_from_untrusted_ca_is_rejected(pki):
    raw = pki.seal("Forged", headers=mail_headers(), signer=pki.mallory_sign)
    with pytest.raises(Rejected, match="verification failed"):
        open_mail(pki, raw)


def test_from_must_match_signer(pki):
    raw = pki.seal("Spoof", headers=mail_headers(From="other@example.com"), signer=pki.author_sign)
    with pytest.raises(Rejected, match="does not match signer"):
        open_mail(pki, raw, allowed=frozenset({AUTHOR, "other@example.com"}))


def test_signer_must_be_allowed(pki):
    raw = pki.seal("Hi", headers=mail_headers(), signer=pki.author_sign)
    with pytest.raises(Rejected, match="not allowed"):
        open_mail(pki, raw, allowed=frozenset({"someone@example.com"}))


def test_stale_signature_is_rejected(pki):
    raw = pki.seal("Old", headers=mail_headers(), signer=pki.author_sign)
    open_mail(pki, raw, now=datetime.now(UTC) + HOUR - timedelta(minutes=1))
    with pytest.raises(Rejected, match="too old"):
        open_mail(pki, raw, now=datetime.now(UTC) + HOUR + timedelta(minutes=1))


def test_signature_without_signing_time_is_rejected(pki):
    raw = pki.seal("No time", headers=mail_headers(), signer=pki.author_sign, signed_attributes=False)
    with pytest.raises(Rejected, match="signingTime"):
        open_mail(pki, raw)


def test_mail_encrypted_for_someone_else_is_rejected(pki):
    raw = pki.seal("Hi", headers=mail_headers(), signer=pki.author_sign)
    # A bridge holding a different key cannot open the bot's mail.
    other_bot = Smime(openssl=OPENSSL, bot_cert_pem=pki.stranger_enc.cert_pem,
                      bot_key_pem=pki.stranger_enc.key_pem, ca_pem=pki.ca.cert_pem)
    with pytest.raises(Rejected, match="cannot decrypt"):
        other_bot.open(raw, allowed_senders=ALLOWED, max_age=HOUR, now=datetime.now(UTC))


def test_reply_cert_selection(pki):
    raw = pki.seal(
        "Pick my cert",
        headers=mail_headers(),
        signer=pki.author_sign,
        extra_certs=[pki.stranger_enc, pki.mallory_enc, pki.author_enc],
    )
    opened = open_mail(pki, raw)
    # Only the author's EC keyAgreement certificate from the trusted CA qualifies.
    assert opened.reply_cert_pem is not None
    assert "".join(opened.reply_cert_pem.split()) == "".join(pki.author_enc.cert_pem.split())


def test_no_reply_cert_without_qualifying_certificate(pki):
    raw = pki.seal("No enc cert", headers=mail_headers(), signer=pki.author_sign,
                   extra_certs=[pki.stranger_enc, pki.mallory_enc])
    assert open_mail(pki, raw).reply_cert_pem is None
