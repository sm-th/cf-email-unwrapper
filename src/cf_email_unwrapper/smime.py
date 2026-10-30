"""S/MIME open/seal and the gateway mailbox signature, through the OpenSSL CLI.

All cryptographic operations run in ``openssl cms`` / ``openssl verify``.
``asn1crypto`` is only used to read certificate fields and CMS signed
attributes (signingTime, embedded certificates).
"""

import email
import email.policy
import email.utils
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from asn1crypto import cms, pem, x509

log = logging.getLogger(__name__)

_FUTURE_SKEW = timedelta(minutes=5)


def mailbox_challenge(address: str) -> bytes:
    """The content signed to open the gateway mailbox of ``address`` (see README)."""
    return f"cf-email-gateway mailbox v1\n{address.strip().lower()}\n".encode()


class Rejected(Exception):
    """The message failed authentication; drop it without a reply."""


class OpenSSLError(Exception):
    pass


@dataclass(frozen=True)
class Opened:
    """An authenticated message."""

    content: bytes  # the signed MIME entity (headers + body)
    sender: str  # From address == signer address, lowercase
    signing_time: datetime
    encrypted: bool
    reply_cert_pem: str | None


@contextmanager
def _workdir() -> Iterator[Path]:
    # mkdtemp creates the directory with mode 0700.
    with tempfile.TemporaryDirectory(prefix="smime-") as d:
        yield Path(d)


def _put(path: Path, data: bytes | str) -> Path:
    if isinstance(data, str):
        data = data.encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _load_cert(data: bytes | str) -> x509.Certificate:
    if isinstance(data, str):
        data = data.encode()
    if pem.detect(data):
        _, _, data = pem.unarmor(data)
    return x509.Certificate.load(data)


def _to_pem(cert: x509.Certificate) -> str:
    return pem.armor("CERTIFICATE", cert.dump()).decode()


def cert_emails(cert: x509.Certificate) -> set[str]:
    emails = set()
    subject_email = cert.subject.native.get("email_address")
    if isinstance(subject_email, str):
        emails.add(subject_email.lower())
    elif subject_email:
        emails.update(e.lower() for e in subject_email)
    san = cert.subject_alt_name_value
    if san is not None:
        emails.update(gn.native.lower() for gn in san if gn.name == "rfc822_name")
    return emails


def _is_pkcs7_mime(msg: email.message.Message) -> bool:
    return msg.get_content_type() in ("application/pkcs7-mime", "application/x-pkcs7-mime")


def _is_enveloped(msg: email.message.Message) -> bool:
    return _is_pkcs7_mime(msg) and (msg.get_param("smime-type") or "").lower() != "signed-data"


def _is_signed(msg: email.message.Message) -> bool:
    if msg.get_content_type() == "multipart/signed":
        protocol = (msg.get_param("protocol") or "").lower()
        return protocol in ("application/pkcs7-signature", "application/x-pkcs7-signature")
    return _is_pkcs7_mime(msg) and (msg.get_param("smime-type") or "").lower() == "signed-data"


def from_address(msg: email.message.Message) -> str:
    _, addr = email.utils.parseaddr(str(msg.get("From", "")))
    return addr.strip().lower()


class Smime:
    def __init__(
        self,
        *,
        openssl: str,
        signing_cert_pem: str,
        signing_key_pem: str,
        encryption_cert_pem: str,
        encryption_key_pem: str,
        ca_pem: str,
    ):
        self._openssl = openssl
        self._signing_cert = signing_cert_pem
        self._signing_key = signing_key_pem
        self._encryption_cert = encryption_cert_pem
        self._encryption_key = encryption_key_pem
        self._ca = ca_pem

    def _run(self, cwd: Path, *args: str) -> None:
        proc = subprocess.run([self._openssl, *args], cwd=cwd, capture_output=True)
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            raise OpenSSLError(f"openssl {' '.join(args[:2])} failed: {stderr}")

    # --- inbound -----------------------------------------------------------

    def open(
        self,
        raw: bytes,
        *,
        allowed_senders: frozenset[str],
        max_age: timedelta,
        now: datetime,
    ) -> Opened:
        outer = email.message_from_bytes(raw, policy=email.policy.default)
        sender = from_address(outer)
        encrypted = _is_enveloped(outer)
        signed_entity = self._decrypt(raw) if encrypted else raw
        inner = email.message_from_bytes(signed_entity, policy=email.policy.default)
        if not _is_signed(inner):
            raise Rejected("message is not signed")

        content, signer, certs, signing_time = self._verify(signed_entity)

        signer_emails = cert_emails(signer)
        if sender not in signer_emails:
            raise Rejected(f"From {sender!r} does not match signer {sorted(signer_emails)}")
        if sender not in allowed_senders:
            raise Rejected(f"sender {sender!r} is not allowed")
        if signing_time is None:
            raise Rejected("signature has no signingTime")
        if signing_time < now - max_age:
            raise Rejected(f"signature is too old ({signing_time.isoformat()})")
        if signing_time > now + _FUTURE_SKEW:
            raise Rejected(f"signingTime is in the future ({signing_time.isoformat()})")

        return Opened(
            content=content,
            sender=sender,
            signing_time=signing_time,
            encrypted=encrypted,
            reply_cert_pem=self.select_reply_cert(certs, sender),
        )

    def _decrypt(self, raw: bytes) -> bytes:
        with _workdir() as d:
            _put(d / "in.eml", raw)
            _put(d / "bot.pem", self._encryption_cert)
            _put(d / "bot.key", self._encryption_key)
            try:
                self._run(d, "cms", "-decrypt", "-inform", "SMIME", "-in", "in.eml",
                          "-recip", "bot.pem", "-inkey", "bot.key", "-out", "out.eml")
            except OpenSSLError as e:
                raise Rejected(f"cannot decrypt: {e}") from e
            return (d / "out.eml").read_bytes()

    def _verify(
        self, signed: bytes
    ) -> tuple[bytes, x509.Certificate, list[x509.Certificate], datetime | None]:
        with _workdir() as d:
            _put(d / "in.eml", signed)
            _put(d / "ca.pem", self._ca)
            try:
                self._run(d, "cms", "-verify", "-inform", "SMIME", "-in", "in.eml",
                          "-CAfile", "ca.pem", "-purpose", "smimesign",
                          "-signer", "signer.pem", "-out", "content.eml")
            except OpenSSLError as e:
                raise Rejected(f"signature verification failed: {e}") from e
            self._run(d, "cms", "-cmsout", "-inform", "SMIME", "-in", "in.eml",
                      "-outform", "DER", "-out", "cms.der")
            content = (d / "content.eml").read_bytes()
            signer_pem = (d / "signer.pem").read_bytes()
            der = (d / "cms.der").read_bytes()

        signer = _load_cert(signer_pem)
        signed_data = cms.ContentInfo.load(der)["content"]
        certs = [c.chosen for c in signed_data["certificates"] if c.name == "certificate"]
        signer_infos = list(signed_data["signer_infos"])
        if len(signer_infos) != 1:
            raise Rejected(f"expected exactly one signer, found {len(signer_infos)}")
        signing_time = None
        for attr in signer_infos[0]["signed_attrs"]:
            if attr["type"].native == "signing_time":
                signing_time = attr["values"][0].native
        return content, signer, certs, signing_time

    def select_reply_cert(self, certs: list[x509.Certificate], address: str) -> str | None:
        """Pick the author's encryption certificate among the signature's certificates.

        Requirements: issued for ``address``, an EC key with keyUsage keyAgreement or an RSA
        key with keyUsage keyEncipherment, and a valid chain to the configured CA. The most
        recently issued match wins.
        """
        candidates = []
        for cert in certs:
            key_usage = cert.key_usage_value
            usage = key_usage.native if key_usage is not None else set()
            algorithm = cert.public_key.algorithm
            if (
                address in cert_emails(cert)
                and (
                    (algorithm == "ec" and "key_agreement" in usage)
                    or (algorithm == "rsa" and "key_encipherment" in usage)
                )
                and self._chains_to_ca(cert, certs)
            ):
                candidates.append(cert)
        if not candidates:
            return None
        best = max(candidates, key=lambda c: c["tbs_certificate"]["validity"]["not_before"].native)
        return _to_pem(best)

    def _chains_to_ca(self, cert: x509.Certificate, pool: list[x509.Certificate]) -> bool:
        with _workdir() as d:
            _put(d / "ca.pem", self._ca)
            _put(d / "cert.pem", _to_pem(cert))
            args = ["verify", "-CAfile", "ca.pem"]
            others = [c for c in pool if c is not cert]
            if others:
                _put(d / "untrusted.pem", "".join(_to_pem(c) for c in others))
                args += ["-untrusted", "untrusted.pem"]
            try:
                self._run(d, *args, "cert.pem")
            except OpenSSLError as e:
                log.info("ignoring certificate %s: %s", cert.subject.human_friendly, e)
                return False
            return True

    # --- outbound ----------------------------------------------------------

    def seal(self, entity: bytes, reply_cert_pem: str | None) -> bytes:
        """Sign ``entity`` (detached, SHA-256) and encrypt it for ``reply_cert_pem`` if given.

        The signature carries the bot encryption certificate, so the author's client learns
        where to encrypt follow-ups. Returns a MIME entity (MIME-Version + Content-* headers
        and body).
        """
        with _workdir() as d:
            _put(d / "in.eml", entity)
            _put(d / "bot.pem", self._signing_cert)
            _put(d / "bot.key", self._signing_key)
            _put(d / "bot-encryption.pem", self._encryption_cert)
            self._run(d, "cms", "-sign", "-in", "in.eml", "-signer", "bot.pem",
                      "-inkey", "bot.key", "-certfile", "bot-encryption.pem", "-md", "sha256",
                      "-outform", "SMIME", "-out", "signed.eml")
            if reply_cert_pem is None:
                return (d / "signed.eml").read_bytes()
            _put(d / "recip.pem", reply_cert_pem)
            # OpenSSL defaults: RSA PKCS#1 v1.5 key transport, or for EC the ECDH KDF
            # dhSinglePass-stdDH-sha1kdf, the only one iOS Mail decrypts.
            self._run(d, "cms", "-encrypt", "-aes256", "-in", "signed.eml",
                      "-outform", "SMIME", "-out", "encrypted.eml", "recip.pem")
            return (d / "encrypted.eml").read_bytes()

    def sign_mailbox_challenge(self, address: str) -> bytes:
        """Detached DER CMS signature of ``mailbox_challenge(address)`` with the bot signing key.

        It embeds the signing certificate and a signingTime, so it must be fresh on every
        connection attempt.
        """
        with _workdir() as d:
            _put(d / "challenge", mailbox_challenge(address))
            _put(d / "bot.pem", self._signing_cert)
            _put(d / "bot.key", self._signing_key)
            self._run(d, "cms", "-sign", "-binary", "-md", "sha256", "-outform", "DER",
                      "-in", "challenge", "-signer", "bot.pem", "-inkey", "bot.key",
                      "-out", "sig.der")
            return (d / "sig.der").read_bytes()
