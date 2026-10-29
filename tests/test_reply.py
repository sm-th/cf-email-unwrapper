import base64
import email
import email.policy

import pytest
from a2a.types.a2a_pb2 import Artifact, Message, Part, Role, Task, TaskState, TaskStatus
from conftest import AUTHOR, BOT

from cf_email_unwrapper.replier import Replier


class FakeOutbox:
    def __init__(self):
        self.pushed: list[dict] = []

    async def find_queue_id(self, name):
        return "outbox-id" if name == "email-outbox" else None

    async def push(self, queue_id, body):
        assert queue_id == "outbox-id"
        self.pushed.append(body)


def make_task(state, *, agent_text="", reply_cert=None, task_id="task-1", artifact_text=""):
    meta = {
        "author": AUTHOR,
        "agentAddress": BOT,
        "subject": "Grüße aus Berlin",
        "messageId": "<parent@example.com>",
        "references": "<root@example.com> <mid@example.com>",
    }
    if reply_cert:
        meta["replyCertPem"] = reply_cert
    user = Message(message_id="m1", role=Role.ROLE_USER, parts=[Part(text="question")])
    user.metadata.update({"email": meta})
    task = Task(id=task_id, context_id="ctx", history=[user])
    task.status.CopyFrom(TaskStatus(state=state))
    if agent_text:
        task.status.message.CopyFrom(
            Message(message_id="m2", role=Role.ROLE_AGENT, parts=[Part(text=agent_text)])
        )
    if artifact_text:
        task.artifacts.append(Artifact(artifact_id="a1", parts=[Part(text=artifact_text)]))
    return task


@pytest.fixture
def outbox():
    return FakeOutbox()


@pytest.fixture
def replier(pki, outbox):
    return Replier(queues=outbox, outbox_queue="email-outbox", smime=pki.smime(), agent_address=BOT)


def parse(body):
    raw = base64.b64decode(body["raw"])
    return raw, email.message_from_bytes(raw, policy=email.policy.default)


async def test_completed_reply_is_encrypted_signed_and_threaded(pki, replier, outbox):
    task = make_task(TaskState.TASK_STATE_COMPLETED, artifact_text="Die Antwort",
                     reply_cert=pki.author_enc.cert_pem)
    await replier.reply_to_task(task)

    [body] = outbox.pushed
    assert (body["version"], body["from"], body["to"]) == (1, BOT, AUTHOR)
    raw, msg = parse(body)
    raw.decode("ascii")  # RFC 2047 keeps the header block ASCII
    assert msg["Subject"] == "Re: Grüße aus Berlin"
    assert msg["From"] == BOT and msg["To"] == AUTHOR
    assert msg["In-Reply-To"] == "<parent@example.com>"
    assert msg["References"].split() == [
        "<root@example.com>", "<mid@example.com>", "<parent@example.com>"
    ]
    assert msg["Message-ID"].endswith("@example.org>")
    assert msg["Auto-Submitted"] == "auto-replied"
    assert (msg["X-Priority"], msg["Importance"]) == ("4 (Low)", "low")
    assert msg.get_param("smime-type") == "enveloped-data"

    content = pki.open_reply(raw, recipient=pki.author_enc)
    text = email.message_from_bytes(content, policy=email.policy.default).get_content()
    assert text.strip() == "Die Antwort"


async def test_failure_reply_is_signed_only_without_cert_and_marked_urgent(pki, replier, outbox):
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_FAILED, agent_text="Tool crashed"))

    raw, msg = parse(outbox.pushed[0])
    assert msg.get_content_type() == "multipart/signed"
    assert (msg["X-Priority"], msg["Importance"]) == ("2 (High)", "high")
    content = pki.open_reply(raw, recipient=None)
    assert b"Tool crashed" in content


async def test_each_task_state_is_answered_once(replier, outbox):
    question = make_task(TaskState.TASK_STATE_INPUT_REQUIRED, agent_text="Which city?")
    await replier.reply_to_task(question)
    await replier.reply_to_task(question)
    assert len(outbox.pushed) == 1
    assert parse(outbox.pushed[0])[1]["Importance"] == "low"

    await replier.reply_to_task(make_task(TaskState.TASK_STATE_COMPLETED, agent_text="Done"))
    assert len(outbox.pushed) == 2


async def test_non_final_states_and_tasks_without_email_metadata_are_ignored(replier, outbox):
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_WORKING, agent_text="…"))
    orphan = make_task(TaskState.TASK_STATE_COMPLETED, agent_text="?")
    del orphan.history[:]
    await replier.reply_to_task(orphan)
    assert outbox.pushed == []


async def test_oversized_answer_becomes_an_urgent_notice(replier, outbox):
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_COMPLETED, agent_text="x" * 200_000))
    [body] = outbox.pushed
    assert len(body["raw"]) < 120_000
    assert parse(body)[1]["Importance"] == "high"
