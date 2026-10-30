"""Configuration from environment variables (documented in README.md)."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from urllib.parse import quote, urlsplit, urlunsplit

_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def config_dir(env: dict[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".config"
    )
    return Path(base) / "cf-email-unwrapper"


def find_env_file(arg: str | None, env: dict[str, str]) -> Path | None:
    """The env file to read: ``arg`` as a name or path, else the only ``*.env`` in the config dir.

    Returns None when no argument is given and the environment already configures the service
    (``AGENT_ADDRESS`` is set), so containers and exported shells keep working unchanged.
    """
    directory = config_dir(env)
    if arg:
        path = Path(arg).expanduser()
        if path.suffix != ".env" and "/" not in arg:
            path = directory / f"{arg}.env"
        if not path.is_file():
            raise ValueError(f"config file {path} not found")
        return path
    if env.get("AGENT_ADDRESS"):
        return None
    files = sorted(directory.glob("*.env"))
    if len(files) == 1:
        return files[0]
    if not files:
        raise ValueError(f"no configuration: set the environment or create {directory}/NAME.env")
    names = ", ".join(f.stem for f in files)
    raise ValueError(f"several config files in {directory} ({names}): pass the name as argument")


def load_env(arg: str | None, env: dict[str, str]) -> dict[str, str]:
    """The environment merged over the env file (variables already set win)."""
    path = find_env_file(arg, env)
    if path is None:
        return dict(env)
    values = {k: v for k, v in dotenv_values(path).items() if v is not None}
    return {**values, **env}


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


_WS_SCHEMES = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}


def mailbox_url(gateway_url: str, address: str) -> str:
    """WebSocket URL of the gateway mailbox for ``address`` (``<GATEWAY_URL>/mailbox/<address>``)."""
    parts = urlsplit(gateway_url.strip())
    scheme = _WS_SCHEMES.get(parts.scheme.lower())
    if scheme is None or not parts.netloc:
        raise ValueError(f"GATEWAY_URL: expected an http(s) or ws(s) URL, got {gateway_url!r}")
    path = parts.path.rstrip("/") + "/mailbox/" + quote(address, safe="")
    return urlunsplit((scheme, parts.netloc, path, "", ""))


@dataclass(frozen=True)
class Config:
    mailbox_url: str
    agent_address: str
    bot_signing_key_pem: str
    bot_signing_cert_pem: str
    bot_encryption_key_pem: str
    bot_encryption_cert_pem: str
    ca_cert_pem: str
    allowed_senders: frozenset[str]
    a2a_url: str | None  # None: demo mode, the guardian answers with an echo itself
    a2a_token: str | None
    push_host: str
    push_port: int
    push_url: str
    push_token: str | None
    max_signature_age: float
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

        agent_address = required("AGENT_ADDRESS").lower()
        state_home = env.get("XDG_STATE_HOME") or os.path.join(
            env.get("HOME") or os.path.expanduser("~"), ".local", "state"
        )
        a2a_url = env.get("A2A_URL", "").strip() or None
        push_host = env.get("PUSH_HOST", "127.0.0.1")
        push_port = int(env.get("PUSH_PORT", "8090"))
        url_host = "127.0.0.1" if push_host in ("0.0.0.0", "::") else push_host
        return cls(
            mailbox_url=mailbox_url(required("GATEWAY_URL"), agent_address),
            agent_address=agent_address,
            bot_signing_key_pem=read_pem("BOT_SIGNING_KEY_FILE", required("BOT_SIGNING_KEY_FILE")),
            bot_signing_cert_pem=read_pem("BOT_SIGNING_CERT_FILE", required("BOT_SIGNING_CERT_FILE")),
            bot_encryption_key_pem=read_pem("BOT_ENCRYPTION_KEY_FILE", required("BOT_ENCRYPTION_KEY_FILE")),
            bot_encryption_cert_pem=read_pem("BOT_ENCRYPTION_CERT_FILE", required("BOT_ENCRYPTION_CERT_FILE")),
            ca_cert_pem=read_pem("CA_CERT_FILE", required("CA_CERT_FILE")),
            allowed_senders=frozenset(
                s.lower() for s in re.split(r"[\s,]+", required("ALLOWED_SENDERS")) if s
            ),
            a2a_url=a2a_url,
            a2a_token=required("A2A_TOKEN") if a2a_url else None,
            push_host=push_host,
            push_port=push_port,
            push_url=env.get("PUSH_URL") or f"http://{url_host}:{push_port}/a2a/push",
            push_token=required("PUSH_TOKEN") if a2a_url else None,
            max_signature_age=parse_duration(env.get("MAX_SIGNATURE_AGE") or "1h"),
            openssl=env.get("OPENSSL") or "openssl",
            state_path=env.get("STATE_PATH")
            or os.path.join(state_home, "cf-email-unwrapper", f"{agent_address}.sqlite3"),
        )
