"""Gateway WebSocket -> S/MIME -> A2A -> push notification -> outbox -> gateway.

The gateway is a real local WebSocket server implementing the mailbox contract; the agent is
mocked at the HTTP transport.
"""

import asyncio
import base64
import binascii
import contextlib
import email
import email.policy
import json
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from asn1crypto import cms
from conftest import AUTHOR, BOT, OPENSSL, mail_headers
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from cf_email_unwrapper.agent import AgentClient, EchoAgent
from cf_email_unwrapper.config import Config, mailbox_url
from cf_email_unwrapper.gateway import SIGNATURE_HEADER, Gateway, decode_frame, encode_frame
from cf_email_unwrapper.outbox import Sender
from cf_email_unwrapper.push import TOKEN_HEADER, create_app
from cf_email_unwrapper.replier import Replier
from cf_email_unwrapper.state import State
from cf_email_unwrapper.worker import Worker

PUSH_URL = "http://127.0.0.1:8090/a2a/push"
PUSH_TOKEN = "stable-push-secret"
MAILBOX_PREFIX = "/mailbox/"
FAST = 0.05  # retry and reconnect delays in tests


def mailbox_signature_error(ca_cert: Path, address: str, header: str) -> str | None:
    """Why ``header`` does not open the mailbox of ``address``, checked like cf-email-gateway."""
    try:
        der = base64.b64decode(header, validate=True)
    except binascii.Error:
        return "signature is not base64"
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "sig.der").write_bytes(der)
        (work / "challenge").write_bytes(f"cf-email-gateway mailbox v1\n{address}\n".encode())
        verify = subprocess.run(
            [OPENSSL, "cms", "-verify", "-binary", "-inform", "DER", "-in", "sig.der",
             "-content", "challenge", "-CAfile", str(ca_cert), "-purpose", "smimesign",
             "-signer", "signer.pem", "-out", "/dev/null"],
            cwd=work, capture_output=True)
        if verify.returncode != 0:
            return f"verification failed: {verify.stderr.decode(errors='replace').strip()}"
        emails = subprocess.run([OPENSSL, "x509", "-in", "signer.pem", "-noout", "-email"],
                                cwd=work, capture_output=True, check=True).stdout.decode()
    if address not in {e.strip().lower() for e in emails.splitlines()}:
        return f"signer certificate is not for {address}: {emails.split()}"
    signer_infos = list(cms.ContentInfo.load(der)["content"]["signer_infos"])
    if len(signer_infos) != 1:
        return f"expected exactly one signer, found {len(signer_infos)}"
    times = [a["values"][0].native for a in signer_infos[0]["signed_attrs"]
             if a["type"].native == "signing_time"]
    if not times or abs(times[0] - datetime.now(UTC)) > timedelta(minutes=5):
        return f"signingTime missing or off by more than 5 minutes: {times}"
    return None


class FakeGateway:
    """The mailbox Durable Object of cf-email-gateway, for one address."""

    def __init__(self, ca_cert: Path):
        self.ca_cert = ca_cert
        self.mails: dict[str, tuple[dict, bytes]] = {}  # stored until acked, oldest first
        self.acked: list[str] = []
        self.sends: list[dict] = []  # every send frame header received
        self.sent: list[tuple[str, bytes]] = []  # (to, raw) handed to send_email
        self.send_errors = 0  # answer this many upcoming sends with an error
        self.connections = 0
        self.rejected: list[str] = []  # reasons of refused connection attempts
        self.socket: ServerConnection | None = None

    async def __aenter__(self):
        self.server = await serve(self.handler, "127.0.0.1", 0, process_request=self.check,
                                  max_size=None)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def check(self, connection, request):
        address = unquote(request.path.removeprefix(MAILBOX_PREFIX)).strip().lower()
        if not request.path.startswith(MAILBOX_PREFIX) or address != BOT:
            return connection.respond(404, "not found\n")
        error = await asyncio.to_thread(mailbox_signature_error, self.ca_cert, address,
                                        request.headers.get(SIGNATURE_HEADER, ""))
        if error is not None:
            self.rejected.append(error)
            return connection.respond(401, "unauthorized\n")
        return None

    async def handler(self, ws: ServerConnection) -> None:
        if self.socket is not None:
            await self.socket.close(4000, "replaced")
        self.socket = ws
        self.connections += 1
        for header, raw in list(self.mails.values()):
            await ws.send(encode_frame(header, raw))
        with contextlib.suppress(ConnectionClosed):
            async for message in ws:
                if isinstance(message, str):
                    frame = json.loads(message)
                    assert frame["type"] == "ack"
                    if self.mails.pop(frame["id"], None) is not None:
                        self.acked.append(frame["id"])
                    continue
                header, raw = decode_frame(message)
                assert header["type"] == "send"
                self.sends.append(header)
                if self.send_errors:
                    self.send_errors -= 1
                    reply = {"type": "error", "id": header["id"], "message": "send_email failed"}
                else:
                    self.sent.append((header["to"], raw))
                    reply = {"type": "sent", "id": header["id"]}
                await ws.send(json.dumps(reply))

    async def deliver(self, raw: bytes) -> str:
        mail_id = str(uuid.uuid4())
        header = {"type": "mail", "id": mail_id, "from": AUTHOR, "to": BOT,
                  "receivedAt": "2026-10-29T08:00:00.000Z", "size": len(raw)}
        self.mails[mail_id] = (header, raw)
        if self.socket is not None:
            with contextlib.suppress(ConnectionClosed):
                await self.socket.send(encode_frame(header, raw))
        return mail_id


class FakeAgent:
    """Minimal A2A v1.0 JSON-RPC agent keeping tasks in memory."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.attempts = 0
        self.unavailable = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer a2a-token"
        self.attempts += 1
        if self.unavailable:
            return httpx.Response(503)
        rpc = json.loads(request.content)
        self.requests.append(rpc)
        params = rpc["params"]
        if rpc["method"] == "SendMessage":
            task_id = f"task-{len(self.tasks) + 1}"
            message = params["message"]
            self.tasks[task_id] = {
                "id": task_id,
                "contextId": message["contextId"],
                "status": {"state": "TASK_STATE_SUBMITTED"},
                "history": [message],
            }
            result = {"task": self.tasks[task_id]}
        elif rpc["method"] == "GetTask":
            result = self.tasks[params["id"]]
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"],
                                             "error": {"code": -32601, "message": "no"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], "result": result})

    def complete(self, task_id: str, text: str) -> None:
        self.tasks[task_id]["status"] = {"state": "TASK_STATE_COMPLETED"}
        self.tasks[task_id]["artifacts"] = [{"artifactId": "a1", "parts": [{"text": text}]}]

    def methods(self) -> list[str]:
        return [r["method"] for r in self.requests]


def load_config(pki, gateway_url, state_path, **env) -> Config:
    return Config.from_env({
        "GATEWAY_URL": gateway_url, "AGENT_ADDRESS": BOT,
        "BOT_SIGNING_KEY_FILE": str(pki.bot_sign.key), "BOT_SIGNING_CERT_FILE": str(pki.bot_sign.cert),
        "BOT_ENCRYPTION_KEY_FILE": str(pki.bot_enc.key),
        "BOT_ENCRYPTION_CERT_FILE": str(pki.bot_enc.cert), "CA_CERT_FILE": str(pki.ca.cert),
        "ALLOWED_SENDERS": AUTHOR, "A2A_URL": "http://agent/", "A2A_TOKEN": "a2a-token",
        "PUSH_URL": PUSH_URL, "PUSH_TOKEN": PUSH_TOKEN, "OPENSSL": OPENSSL,
        "STATE_PATH": str(state_path), **env,
    })


class Unwrapper:
    """One unwrapper process: fresh config, clients, state, gateway connection and push app."""

    def __init__(self, pki, gateway_url, agent, state_path, signing=None):
        self.config = load_config(pki, gateway_url, state_path)
        a2a = AgentClient(self.config.a2a_url, self.config.a2a_token,
                          httpx.AsyncClient(transport=httpx.MockTransport(agent.handler)))
        smime = pki.smime(signing)
        self.state = State(self.config.state_path)
        self.sender = Sender(self.state, retry_delay=FAST)
        replier = Replier(smime=smime, agent_address=BOT, state=self.state,
                          on_queued=self.sender.wake)
        self.worker = Worker(config=self.config, smime=smime, agent=a2a, replier=replier,
                             state=self.state, retry_delay=FAST)
        self.gateway = Gateway(self.config.mailbox_url,
                               lambda: smime.sign_mailbox_challenge(self.config.agent_address),
                               on_mail=self.worker.handle, on_connect=self.sender.wake,
                               min_delay=FAST, max_delay=FAST)
        app = create_app(token=self.config.push_token, agent=a2a, replier=replier)
        self.push = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://push")
        self._tasks = [asyncio.create_task(self.gateway.run()),
                       asyncio.create_task(self.sender.run(self.gateway))]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.state.close()


async def eventually(condition, timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state" / "hermes.sqlite3"


@pytest.fixture
async def gateway(pki):
    async with FakeGateway(pki.ca.cert) as gateway:
        yield gateway


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
async def start(pki, gateway, agent, state_path):
    started: list[Unwrapper] = []

    async def start_unwrapper() -> Unwrapper:
        unwrapper = Unwrapper(pki, gateway.url, agent, state_path)
        started.append(unwrapper)
        await eventually(lambda: unwrapper.gateway.connected)
        return unwrapper

    yield start_unwrapper
    for unwrapper in started:
        with contextlib.suppress(Exception):
            await unwrapper.stop()


def test_mailbox_url_maps_the_scheme_and_encodes_the_address():
    assert (mailbox_url("https://gw.example.workers.dev/", "a+b@example.org")
            == "wss://gw.example.workers.dev/mailbox/a%2Bb%40example.org")
    assert mailbox_url("http://127.0.0.1:8787", BOT) == "ws://127.0.0.1:8787/mailbox/hermes%40example.org"
    assert mailbox_url("wss://gw/base", BOT) == "wss://gw/base/mailbox/hermes%40example.org"
    with pytest.raises(ValueError, match="GATEWAY_URL"):
        mailbox_url("ftp://gw", BOT)


async def test_mail_round_trip_produces_exactly_one_reply(pki, gateway, agent, start):
    unwrapper = await start()
    raw = pki.seal("What is the weather?", signer=pki.author_sign, extra_certs=[pki.author_enc_rsa],
                   headers=mail_headers(Subject="Weather", **{"Message-ID": "<q1@example.com>"}))
    mail_id = await gateway.deliver(raw)
    await eventually(lambda: gateway.acked == [mail_id])

    [send] = agent.requests
    assert send["method"] == "SendMessage"
    message, configuration = send["params"]["message"], send["params"]["configuration"]
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": "What is the weather?"}]
    assert "metadata" not in message  # the email context stays in the unwrapper
    assert configuration["returnImmediately"] is True
    assert configuration["taskPushNotificationConfig"] == {"url": PUSH_URL, "token": PUSH_TOKEN}
    assert gateway.mails == {} and gateway.sends == []

    notification = {"statusUpdate": {"taskId": "task-1", "contextId": message["contextId"],
                                     "status": {"state": "TASK_STATE_COMPLETED"}}}
    forged = await unwrapper.push.post("/a2a/push", json=notification, headers={TOKEN_HEADER: "wrong"})
    assert forged.status_code == 401

    agent.complete("task-1", "Sunny.")
    for _ in range(2):  # duplicate notification
        resp = await unwrapper.push.post("/a2a/push", json=notification,
                                         headers={TOKEN_HEADER: PUSH_TOKEN})
        assert resp.status_code == 200

    await eventually(lambda: unwrapper.state.outbox() == [] and gateway.sent)
    [(to, reply)] = gateway.sent
    assert to == AUTHOR
    headers = email.message_from_bytes(reply, policy=email.policy.default)
    assert headers["Subject"] == "Re: Weather"
    assert headers["In-Reply-To"] == "<q1@example.com>"
    content = pki.open_reply(reply, recipient=pki.author_enc_rsa)
    assert b"Sunny." in content
    await asyncio.sleep(0.2)
    assert len(gateway.sends) == 1


async def test_demo_mode_echoes_without_an_agent(pki, gateway, state_path, capsys):
    config = load_config(pki, gateway.url, state_path, A2A_URL="", A2A_TOKEN="", PUSH_TOKEN="")
    assert config.a2a_url is None and config.push_token is None
    smime = pki.smime()
    state = State(config.state_path)
    sender = Sender(state, retry_delay=FAST)
    replier = Replier(smime=smime, agent_address=BOT, state=state, on_queued=sender.wake)
    worker = Worker(config=config, smime=smime, agent=EchoAgent(), replier=replier, state=state,
                    retry_delay=FAST)
    client = Gateway(config.mailbox_url, lambda: smime.sign_mailbox_challenge(BOT),
                     on_mail=worker.handle, on_connect=sender.wake, min_delay=FAST, max_delay=FAST)
    tasks = [asyncio.create_task(client.run()), asyncio.create_task(sender.run(client))]
    try:
        raw = pki.seal("Ping", signer=pki.author_sign, extra_certs=[pki.author_enc_rsa],
                       headers=mail_headers())
        mail_id = await gateway.deliver(raw)
        await eventually(lambda: gateway.acked == [mail_id] and gateway.sent)
        [(to, reply)] = gateway.sent
        assert to == AUTHOR
        assert b"Echo: Ping" in pki.open_reply(reply, recipient=pki.author_enc_rsa)
        assert "Ping" in capsys.readouterr().out
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        state.close()


async def test_task_finished_while_down_is_answered_once_after_restart(
    pki, gateway, agent, start, state_path
):
    unwrapper = await start()
    raw = pki.seal("Long job", signer=pki.author_sign, extra_certs=[pki.author_enc],
                   headers=mail_headers(**{"Message-ID": "<long@example.com>"}))
    mail_id = await gateway.deliver(raw)
    await eventually(lambda: gateway.acked == [mail_id])
    await unwrapper.worker.recover()  # still running: nothing to answer
    assert unwrapper.state.outbox() == []
    await unwrapper.stop()

    # The agent finishes while the unwrapper is down, so its notification is lost.
    agent.complete("task-1", "Finished.")
    restarted = await start()
    await restarted.worker.recover()
    await eventually(lambda: gateway.sent)
    [(_, reply)] = gateway.sent
    assert email.message_from_bytes(reply)["In-Reply-To"] == "<long@example.com>"
    assert b"Finished." in pki.open_reply(reply, recipient=pki.author_enc)

    # A late notification and another restart do not answer again.
    resp = await restarted.push.post(
        "/a2a/push", json={"task": {"id": "task-1"}}, headers={TOKEN_HEADER: PUSH_TOKEN}
    )
    assert resp.status_code == 200
    await restarted.stop()
    again = await start()
    await again.worker.recover()
    await asyncio.sleep(0.2)
    assert len(gateway.sent) == 1 and again.state.outbox() == []
    assert agent.methods().count("GetTask") == 3  # the last recover skips the closed task

    with pytest.raises(ValueError, match="PUSH_TOKEN"):
        load_config(pki, gateway.url, state_path, PUSH_TOKEN="  ")
    with pytest.raises(ValueError, match="BOT_SIGNING_CERT_FILE"):
        load_config(pki, gateway.url, state_path, BOT_SIGNING_CERT_FILE="")
    with pytest.raises(ValueError, match="CA_CERT_FILE: cannot read"):
        load_config(pki, gateway.url, state_path, CA_CERT_FILE=str(state_path) + ".missing")


async def test_duplicate_mail_reaches_the_agent_once(pki, gateway, agent, start):
    await start()
    raw = pki.seal("hi", signer=pki.author_sign, headers=mail_headers())
    # The gateway received the same email twice; both copies arrive at once.
    first, again = await gateway.deliver(raw), await gateway.deliver(raw)

    await eventually(lambda: sorted(gateway.acked) == sorted([first, again]))
    assert agent.methods() == ["SendMessage"]


async def test_unauthenticated_mail_is_acked_without_reply(pki, gateway, agent, start):
    await start()
    mail_id = await gateway.deliver(pki.seal("hi", signer=pki.mallory_sign, headers=mail_headers()))

    await eventually(lambda: gateway.acked == [mail_id])
    assert agent.attempts == 0 and gateway.sends == []


async def test_unavailable_agent_is_retried_before_the_ack(pki, gateway, agent, start):
    await start()
    agent.unavailable = True
    mail_id = await gateway.deliver(pki.seal("hi", signer=pki.author_sign, headers=mail_headers()))

    await eventually(lambda: agent.attempts >= 3)
    assert gateway.acked == [] and mail_id in gateway.mails

    agent.unavailable = False
    await eventually(lambda: gateway.acked == [mail_id])
    assert agent.methods() == ["SendMessage"]


async def test_gateway_error_keeps_the_reply_for_a_retry(pki, gateway, agent, start):
    unwrapper = await start()
    mail_id = await gateway.deliver(pki.seal("hi", signer=pki.author_sign, headers=mail_headers()))
    await eventually(lambda: gateway.acked == [mail_id])

    gateway.send_errors = 1
    agent.complete("task-1", "Answer.")
    resp = await unwrapper.push.post("/a2a/push", json={"task": {"id": "task-1"}},
                                     headers={TOKEN_HEADER: PUSH_TOKEN})
    assert resp.status_code == 200  # stored locally

    await eventually(lambda: len(gateway.sends) == 1)
    await eventually(lambda: [r.attempts for r in unwrapper.state.outbox()] == [1])
    await eventually(lambda: unwrapper.state.outbox() == [])
    assert len(gateway.sends) == 2 and len(gateway.sent) == 1
    assert gateway.sends[0]["id"] == gateway.sends[1]["id"]


async def test_reconnect_resends_unacked_mail_and_continues(pki, gateway, agent, start):
    unwrapper = await start()
    agent.unavailable = True
    stuck = await gateway.deliver(pki.seal("one", signer=pki.author_sign, headers=mail_headers()))
    await eventually(lambda: agent.attempts >= 1)

    await gateway.socket.close(1011, "restarting")
    await eventually(lambda: gateway.connections == 2 and unwrapper.gateway.connected)

    agent.unavailable = False
    await eventually(lambda: gateway.acked == [stuck])
    fresh = await gateway.deliver(pki.seal("two", signer=pki.author_sign, headers=mail_headers()))
    await eventually(lambda: gateway.acked == [stuck, fresh])
    # The resent copy of the stuck mail was not handled a second time.
    assert agent.methods() == ["SendMessage", "SendMessage"]


async def test_signing_cert_for_another_address_is_refused(pki, gateway, agent, state_path):
    await gateway.deliver(pki.seal("Hello agent", headers=mail_headers(), signer=pki.author_sign))
    unwrapper = Unwrapper(pki, gateway.url, agent, state_path, signing=pki.stranger_sign)
    try:
        await eventually(lambda: len(gateway.rejected) >= 3)  # it keeps reconnecting
        assert all("not for hermes@example.org" in r for r in gateway.rejected)
        assert gateway.connections == 0 and not unwrapper.gateway.connected
        assert len(gateway.mails) == 1 and agent.attempts == 0
    finally:
        await unwrapper.stop()
