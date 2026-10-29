import base64
import email
import email.policy

import pytest
from a2a.types.a2a_pb2 import Artifact, Message, Part, Role, Task, TaskState, TaskStatus
from conftest import AUTHOR, BOT

from cf_email_unwrapper.replier import Replier
from cf_email_unwrapper.state import Mail, State


class FakeOutbox:
    def __init__(self):
        self.pushed: list[dict] = []

    async def find_queue_id(self, name):
        return "outbox-id" if name == "email-outbox" else None

    async def push(self, queue_id, body):
        assert queue_id == "outbox-id"
        self.pushed.append(body)


def submit(state: State, task_id="task-1", reply_cert=None) -> None:
    """Record the mail the unwrapper handed to the agent as ``task_id``."""
    message_id = f"<{task_id}@example.com>" if task_id != "task-1" else "<parent@example.com>"
    state.record_mail(Mail(
        message_id=message_id,
        author=AUTHOR,
        subject="Grüße aus Berlin",
        references="<root@example.com> <mid@example.com>",
        reply_cert_pem=reply_cert,
    ))
    state.attach_task(message_id, task_id)


def make_task(state, *, agent_text="", task_id="task-1", artifact_text=""):
    user = Message(message_id="m1", role=Role.ROLE_USER, parts=[Part(text="question")])
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
def state():
    state = State(":memory:")
    yield state
    state.close()


@pytest.fixture
def replier(pki, outbox, state):
    return Replier(queues=outbox, outbox_queue="email-outbox", smime=pki.smime(), agent_address=BOT,
                   state=state)


def parse(body):
    raw = base64.b64decode(body["raw"])
    return raw, email.message_from_bytes(raw, policy=email.policy.default)


async def test_completed_reply_is_encrypted_signed_and_threaded(pki, replier, outbox, state):
    submit(state, reply_cert=pki.author_enc.cert_pem)
    task = make_task(TaskState.TASK_STATE_COMPLETED, artifact_text="Die Antwort")
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


async def test_failure_reply_is_signed_only_without_cert_and_marked_urgent(pki, replier, outbox,
                                                                          state):
    submit(state)
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_FAILED, agent_text="Tool crashed"))

    raw, msg = parse(outbox.pushed[0])
    assert msg.get_content_type() == "multipart/signed"
    assert (msg["X-Priority"], msg["Importance"]) == ("2 (High)", "high")
    content = pki.open_reply(raw, recipient=None)
    assert b"Tool crashed" in content


async def test_each_task_state_is_answered_once(replier, outbox, state):
    submit(state)
    question = make_task(TaskState.TASK_STATE_INPUT_REQUIRED, agent_text="Which city?")
    await replier.reply_to_task(question)
    await replier.reply_to_task(question)
    assert len(outbox.pushed) == 1
    assert parse(outbox.pushed[0])[1]["Importance"] == "low"
    assert state.open_tasks() == ["task-1"]  # input-required does not end the task

    await replier.reply_to_task(make_task(TaskState.TASK_STATE_COMPLETED, agent_text="Done"))
    assert len(outbox.pushed) == 2
    assert state.open_tasks() == []


async def test_non_final_states_and_unknown_tasks_are_ignored(replier, outbox, state):
    submit(state)
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_WORKING, agent_text="…"))
    foreign = make_task(TaskState.TASK_STATE_COMPLETED, agent_text="?", task_id="not-ours")
    await replier.reply_to_task(foreign)
    assert outbox.pushed == []


async def test_oversized_answer_becomes_an_urgent_notice(replier, outbox, state):
    submit(state)
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_COMPLETED, agent_text="x" * 200_000))
    [body] = outbox.pushed
    assert len(body["raw"]) < 120_000
    assert parse(body)[1]["Importance"] == "high"
