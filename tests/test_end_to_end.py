"""Queue -> R2 -> S/MIME -> A2A -> push notification -> outbox, with HTTP mocked at the transport."""

import base64
import email
import email.policy
import json

import httpx
import pytest
from conftest import AUTHOR, BOT, OPENSSL, mail_headers

from cf_email_unwrapper.agent import AgentClient
from cf_email_unwrapper.cloudflare import Queues
from cf_email_unwrapper.config import Config
from cf_email_unwrapper.push import TOKEN_HEADER, create_app
from cf_email_unwrapper.replier import Replier
from cf_email_unwrapper.state import State
from cf_email_unwrapper.worker import Worker

INBOUND_ID, OUTBOX_ID = "q-inbound", "q-outbox"
PUSH_URL = "http://127.0.0.1:8090/a2a/push"


class FakeCloudflare:
    """Queues REST API: list, pull (base64 JSON bodies like the real API), ack, push."""

    def __init__(self):
        self.pending: list[dict] = []
        self.acks: list[dict] = []
        self.outbox: list[dict] = []

    def enqueue(self, body: dict) -> None:
        encoded = base64.b64encode(json.dumps(body).encode()).decode()
        self.pending.append({"id": f"m{len(self.pending)}", "lease_id": f"lease-{len(self.pending)}",
                             "attempts": 1, "body": encoded, "timestamp_ms": 0})

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer cf-token"
        path = request.url.path.removeprefix("/client/v4/accounts/acct/queues")
        if request.method == "GET" and path == "":
            queues = [{"queue_id": INBOUND_ID, "queue_name": "mail-hermes-example-org"},
                      {"queue_id": OUTBOX_ID, "queue_name": "email-outbox"}]
            return self.ok(queues, result_info={"page": 1, "total_pages": 1})
        body = json.loads(request.content)
        if path == f"/{INBOUND_ID}/messages/pull":
            messages, self.pending = self.pending, []
            return self.ok({"messages": messages})
        if path == f"/{INBOUND_ID}/messages/ack":
            self.acks.append(body)
            return self.ok({})
        if path == f"/{OUTBOX_ID}/messages":
            assert body["content_type"] == "json"
            self.outbox.append(body["body"])
            return self.ok(None)
        return httpx.Response(404, json={"success": False, "errors": [{"message": path}]})

    @staticmethod
    def ok(result, **extra) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errors": [], "result": result, **extra})


class FakeAgent:
    """Minimal A2A v1.0 JSON-RPC agent keeping tasks in memory."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.unavailable = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer a2a-token"
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


class FakeR2:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get(self, key):
        return self.objects.get(key)

    def delete(self, key):
        self.objects.pop(key, None)


PUSH_TOKEN = "stable-push-secret"


def start_unwrapper(pki, cf, agent, r2, state_path, **env):
    """One unwrapper process: fresh Config, clients, state connection and push app."""
    config = Config.from_env({
        "CF_ACCOUNT_ID": "acct", "CF_API_TOKEN": "cf-token", "R2_ENDPOINT": "http://r2",
        "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s", "AGENT_ADDRESS": BOT,
        "BOT_KEY": pki.bot.key_pem, "BOT_CERT": pki.bot.cert_pem, "CA_CERT": pki.ca.cert_pem,
        "ALLOWED_SENDERS": AUTHOR, "A2A_URL": "http://agent/", "A2A_TOKEN": "a2a-token",
        "PUSH_URL": PUSH_URL, "PUSH_TOKEN": PUSH_TOKEN, "POLL_INTERVAL": "0.01",
        "OPENSSL": OPENSSL, "STATE_PATH": str(state_path), **env,
    })
    queues = Queues("acct", "cf-token", httpx.AsyncClient(transport=httpx.MockTransport(cf.handler)))
    a2a = AgentClient(config.a2a_url, config.a2a_token,
                      httpx.AsyncClient(transport=httpx.MockTransport(agent.handler)))
    smime = pki.smime()
    state = State(config.state_path)
    replier = Replier(queues=queues, outbox_queue="email-outbox", smime=smime, agent_address=BOT,
                      state=state)
    worker = Worker(config=config, queues=queues, store=r2, smime=smime, agent=a2a,
                    replier=replier, state=state)
    app = create_app(token=config.push_token, agent=a2a, replier=replier)
    push = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://push")
    return config, worker, push


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state" / "hermes.sqlite3"


@pytest.fixture
def bridge(pki, state_path):
    cf, agent, r2 = FakeCloudflare(), FakeAgent(), FakeR2()
    config, worker, push = start_unwrapper(pki, cf, agent, r2, state_path)
    return cf, agent, r2, config, worker, push


def deliver(cf, r2, raw: bytes, key: str) -> None:
    r2.objects[key] = raw
    cf.enqueue({"version": 1, "r2Key": key, "from": AUTHOR, "to": BOT,
                "receivedAt": "2026-10-29T08:00:00.000Z", "size": len(raw)})


async def test_mail_round_trip_produces_exactly_one_reply(pki, bridge):
    cf, agent, r2, config, worker, push = bridge
    key = "mail-hermes-example-org/1.eml"
    raw = pki.seal("What is the weather?", signer=pki.author_sign, extra_certs=[pki.author_enc],
                   headers=mail_headers(Subject="Weather", **{"Message-ID": "<q1@example.com>"}))
    deliver(cf, r2, raw, key)

    queue_id = await worker.wait_for_queue()
    assert await worker.poll_once(queue_id) == 1

    [send] = agent.requests
    assert send["method"] == "SendMessage"
    message, configuration = send["params"]["message"], send["params"]["configuration"]
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": "What is the weather?"}]
    assert "metadata" not in message  # the email context stays in the unwrapper
    assert configuration["returnImmediately"] is True
    assert configuration["taskPushNotificationConfig"] == {"url": PUSH_URL, "token": config.push_token}
    # Accepted: acknowledged and removed from R2, nothing replied yet.
    assert cf.acks == [{"acks": [{"lease_id": "lease-0"}], "retries": []}]
    assert key not in r2.objects and cf.outbox == []

    notification = {"statusUpdate": {"taskId": "task-1", "contextId": message["contextId"],
                                     "status": {"state": "TASK_STATE_COMPLETED"}}}
    forged = await push.post("/a2a/push", json=notification, headers={TOKEN_HEADER: "wrong"})
    assert forged.status_code == 401

    agent.complete("task-1", "Sunny.")
    for _ in range(2):  # duplicate notification
        resp = await push.post("/a2a/push", json=notification,
                               headers={TOKEN_HEADER: config.push_token})
        assert resp.status_code == 200

    [outgoing] = cf.outbox
    assert (outgoing["from"], outgoing["to"]) == (BOT, AUTHOR)
    reply = base64.b64decode(outgoing["raw"])
    headers = email.message_from_bytes(reply, policy=email.policy.default)
    assert headers["Subject"] == "Re: Weather"
    assert headers["In-Reply-To"] == "<q1@example.com>"
    content = pki.open_reply(reply, recipient=pki.author_enc)
    assert b"Sunny." in content


async def test_task_finished_while_down_is_answered_once_after_restart(pki, bridge, state_path):
    cf, agent, r2, _, worker, _ = bridge
    raw = pki.seal("Long job", signer=pki.author_sign, extra_certs=[pki.author_enc],
                   headers=mail_headers(**{"Message-ID": "<long@example.com>"}))
    deliver(cf, r2, raw, "k.eml")
    await worker.poll_once(INBOUND_ID)
    await worker.recover()  # still running: nothing to answer
    assert cf.outbox == []

    # The agent finishes while the unwrapper is down, so its notification is lost.
    agent.complete("task-1", "Finished.")
    _, restarted, restarted_push = start_unwrapper(pki, cf, agent, r2, state_path)
    await restarted.recover()
    [outgoing] = cf.outbox
    reply = base64.b64decode(outgoing["raw"])
    assert email.message_from_bytes(reply)["In-Reply-To"] == "<long@example.com>"
    assert b"Finished." in pki.open_reply(reply, recipient=pki.author_enc)

    # A late notification and another restart do not answer again.
    resp = await restarted_push.post(
        "/a2a/push", json={"task": {"id": "task-1"}}, headers={TOKEN_HEADER: PUSH_TOKEN}
    )
    assert resp.status_code == 200
    _, again, _ = start_unwrapper(pki, cf, agent, r2, state_path)
    await again.recover()
    assert len(cf.outbox) == 1
    assert [r["method"] for r in agent.requests].count("GetTask") == 3  # the last recover skips the closed task

    with pytest.raises(ValueError, match="PUSH_TOKEN"):
        start_unwrapper(pki, cf, agent, r2, state_path, PUSH_TOKEN="  ")


async def test_redelivered_mail_reaches_the_agent_once(pki, bridge):
    cf, agent, r2, _, worker, _ = bridge
    raw = pki.seal("hi", signer=pki.author_sign, headers=mail_headers())
    deliver(cf, r2, raw, "first.eml")
    deliver(cf, r2, raw, "again.eml")  # the queue delivers at least once

    assert await worker.poll_once(INBOUND_ID) == 2
    assert [r["method"] for r in agent.requests] == ["SendMessage"]
    assert len(cf.acks) == 2 and r2.objects == {}


async def test_unauthenticated_mail_is_dropped_without_reply(pki, bridge):
    cf, agent, r2, _, worker, _ = bridge
    key = "mail-hermes-example-org/2.eml"
    deliver(cf, r2, pki.seal("hi", signer=pki.mallory_sign, headers=mail_headers()), key)

    assert await worker.poll_once(INBOUND_ID) == 1
    assert agent.requests == []
    assert cf.acks == [{"acks": [{"lease_id": "lease-0"}], "retries": []}]
    assert key not in r2.objects and cf.outbox == []


async def test_agent_failure_retries_and_keeps_the_mail(pki, bridge):
    cf, agent, r2, _, worker, _ = bridge
    agent.unavailable = True
    key = "mail-hermes-example-org/3.eml"
    deliver(cf, r2, pki.seal("hi", signer=pki.author_sign, headers=mail_headers()), key)

    await worker.poll_once(INBOUND_ID)
    assert cf.acks == [{"acks": [], "retries": [{"lease_id": "lease-0", "delay_seconds": 30}]}]
    assert key in r2.objects
