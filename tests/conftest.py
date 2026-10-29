"""Throwaway test PKI (EC P-256) and helpers to seal test mail with the OpenSSL CLI."""

import os
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cf_email_unwrapper.smime import Smime

OPENSSL = os.environ.get("OPENSSL", "openssl")
AUTHOR = "author@example.com"
BOT = "hermes@example.org"


def openssl(*args: str, cwd: Path, input: bytes | None = None) -> bytes:
    proc = subprocess.run([OPENSSL, *args], cwd=cwd, input=input, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"openssl {' '.join(args)}: {proc.stderr.decode()}")
    return proc.stdout


@dataclass(frozen=True)
class Identity:
    cert: Path
    key: Path

    @property
    def cert_pem(self) -> str:
        return self.cert.read_text()

    @property
    def key_pem(self) -> str:
        return self.key.read_text()


class Pki:
    def __init__(self, root: Path):
        self.root = root
        self.ca = self._ca("ca")
        self.other_ca = self._ca("other-ca")
        sign_usage = "digitalSignature,nonRepudiation"
        self.author_sign = self._leaf("author-sign", self.ca, AUTHOR, sign_usage)
        self.author_enc = self._leaf("author-enc", self.ca, AUTHOR, "keyAgreement")
        self.bot = self._leaf("bot", self.ca, BOT, f"{sign_usage},keyAgreement")
        self.mallory_sign = self._leaf("mallory-sign", self.other_ca, AUTHOR, sign_usage)
        self.mallory_enc = self._leaf("mallory-enc", self.other_ca, AUTHOR, "keyAgreement")
        self.stranger_enc = self._leaf("stranger-enc", self.ca, "stranger@example.com", "keyAgreement")

    def _ca(self, name: str) -> Identity:
        openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.pem", "-days", "2",
                "-subj", f"/CN={name}", "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=self.root)
        return Identity(self.root / f"{name}.pem", self.root / f"{name}.key")

    def _leaf(self, name: str, ca: Identity, address: str, key_usage: str) -> Identity:
        (self.root / f"{name}.cnf").write_text(
            "[v3]\nbasicConstraints=CA:FALSE\n"
            f"keyUsage=critical,{key_usage}\nextendedKeyUsage=emailProtection\n"
            f"subjectAltName=email:{address}\n"
            "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
        )
        openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}",
                cwd=self.root)
        openssl("x509", "-req", "-in", f"{name}.csr", "-CA", str(ca.cert), "-CAkey", str(ca.key),
                "-set_serial", str(secrets.randbits(63)), "-days", "1",
                "-extfile", f"{name}.cnf", "-extensions", "v3", "-out", f"{name}.pem",
                cwd=self.root)
        return Identity(self.root / f"{name}.pem", self.root / f"{name}.key")

    def smime(self) -> Smime:
        return Smime(openssl=OPENSSL, bot_cert_pem=self.bot.cert_pem,
                     bot_key_pem=self.bot.key_pem, ca_pem=self.ca.cert_pem)

    def seal(
        self,
        text: str,
        *,
        headers: dict[str, str],
        signer: Identity | None,
        extra_certs: list[Identity] = (),
        encrypt: bool = True,
        signed_attributes: bool = True,
    ) -> bytes:
        """``openssl cms -sign | openssl cms -encrypt`` (for the bot), plus mail headers."""
        work = self.root / f"mail-{secrets.token_hex(4)}"
        work.mkdir()
        entity = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + text.encode() + b"\r\n"
        (work / "body.eml").write_bytes(entity)
        current = "body.eml"
        if signer is not None:
            args = ["cms", "-sign", "-in", current, "-signer", str(signer.cert), "-inkey",
                    str(signer.key), "-md", "sha256", "-out", "signed.eml"]
            if extra_certs:
                (work / "extra.pem").write_text("".join(c.cert_pem for c in extra_certs))
                args += ["-certfile", "extra.pem"]
            if not signed_attributes:
                args.append("-noattr")
            openssl(*args, cwd=work)
            current = "signed.eml"
        if encrypt:
            openssl("cms", "-encrypt", "-aes256", "-in", current, "-out", "enc.eml",
                    str(self.bot.cert), cwd=work)
            current = "enc.eml"
        sealed = (work / current).read_bytes()
        if current == "body.eml":
            sealed = b"MIME-Version: 1.0\r\n" + sealed
        head = "".join(f"{k}: {v}\r\n" for k, v in headers.items()).encode()
        return head + sealed

    def open_reply(self, raw: bytes, recipient: Identity | None) -> bytes:
        """Decrypt (if ``recipient``) and verify a reply; return the signed content."""
        work = self.root / f"reply-{secrets.token_hex(4)}"
        work.mkdir()
        (work / "reply.eml").write_bytes(raw)
        current = "reply.eml"
        if recipient is not None:
            openssl("cms", "-decrypt", "-in", current, "-recip", str(recipient.cert),
                    "-inkey", str(recipient.key), "-out", "dec.eml", cwd=work)
            current = "dec.eml"
        openssl("cms", "-verify", "-in", current, "-CAfile", str(self.ca.cert),
                "-purpose", "smimesign", "-out", "content.eml", cwd=work)
        return (work / "content.eml").read_bytes()


@pytest.fixture(scope="session")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    return Pki(tmp_path_factory.mktemp("pki"))


def mail_headers(**overrides: str) -> dict[str, str]:
    headers = {
        "From": f"Author <{AUTHOR}>",
        "To": BOT,
        "Subject": "Hello agent",
        "Message-ID": f"<{secrets.token_hex(8)}@example.com>",
        "Date": "Thu, 29 Oct 2026 08:00:00 +0000",
    }
    headers.update(overrides)
    return headers
