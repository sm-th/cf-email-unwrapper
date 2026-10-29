"""Configuration from environment variables (documented in README.md)."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def read_pem(variable: str, path: str) -> str:
    """Contents of the PEM file named by ``variable``."""
    try:
        text = Path(path).expanduser().read_text()
    except OSError as e:
        raise ValueError(f"{variable}: cannot read {path}: {e.strerror}") from e
    if not _PEM_BLOCK.search(text):
        raise ValueError(f"{variable}: no PEM block in {path}")
    return text


def parse_duration(value: str) -> float:
    """Seconds, optionally suffixed with s, m or h (``3600``, ``90m``, ``1h``)."""
    value = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if value and value[-1] in units:
        return float(value[:-1]) * units[value[-1]]
    return float(value)


@dataclass(frozen=True)
class Config:
    cf_account_id: str
    cf_api_token: str
    r2_endpoint: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    agent_address: str
    bot_signing_key_pem: str
    bot_signing_cert_pem: str
    bot_encryption_key_pem: str
    bot_encryption_cert_pem: str
    ca_cert_pem: str
    allowed_senders: frozenset[str]
    a2a_url: str
    a2a_token: str
    push_host: str
    push_port: int
    push_url: str
    push_token: str
    outbox_queue: str
    max_signature_age: float
    poll_interval: float
    openssl: str
    state_path: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError(f"missing environment variable {name}")
            return value

        account_id = required("CF_ACCOUNT_ID")
        agent_address = required("AGENT_ADDRESS").lower()
        state_home = env.get("XDG_STATE_HOME") or os.path.join(
            env.get("HOME") or os.path.expanduser("~"), ".local", "state"
        )
        push_host = env.get("PUSH_HOST", "127.0.0.1")
        push_port = int(env.get("PUSH_PORT", "8090"))
        url_host = "127.0.0.1" if push_host in ("0.0.0.0", "::") else push_host
        return cls(
            cf_account_id=account_id,
            cf_api_token=required("CF_API_TOKEN"),
            r2_endpoint=env.get("R2_ENDPOINT") or f"https://{account_id}.r2.cloudflarestorage.com",
            r2_access_key_id=required("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=required("R2_SECRET_ACCESS_KEY"),
            r2_bucket=env.get("R2_BUCKET") or "email-agents",
            agent_address=agent_address,
            bot_signing_key_pem=read_pem("BOT_SIGNING_KEY_FILE", required("BOT_SIGNING_KEY_FILE")),
            bot_signing_cert_pem=read_pem("BOT_SIGNING_CERT_FILE", required("BOT_SIGNING_CERT_FILE")),
            bot_encryption_key_pem=read_pem("BOT_ENCRYPTION_KEY_FILE", required("BOT_ENCRYPTION_KEY_FILE")),
            bot_encryption_cert_pem=read_pem("BOT_ENCRYPTION_CERT_FILE", required("BOT_ENCRYPTION_CERT_FILE")),
            ca_cert_pem=read_pem("CA_CERT_FILE", required("CA_CERT_FILE")),
            allowed_senders=frozenset(
                s.lower() for s in re.split(r"[\s,]+", required("ALLOWED_SENDERS")) if s
            ),
            a2a_url=required("A2A_URL"),
            a2a_token=required("A2A_TOKEN"),
            push_host=push_host,
            push_port=push_port,
            push_url=env.get("PUSH_URL") or f"http://{url_host}:{push_port}/a2a/push",
            push_token=required("PUSH_TOKEN"),
            outbox_queue=env.get("OUTBOX_QUEUE") or "email-outbox",
            max_signature_age=parse_duration(env.get("MAX_SIGNATURE_AGE") or "1h"),
            poll_interval=parse_duration(env.get("POLL_INTERVAL") or "5"),
            openssl=env.get("OPENSSL") or "openssl",
            state_path=env.get("STATE_PATH")
            or os.path.join(state_home, "cf-email-unwrapper", f"{agent_address}.sqlite3"),
        )
