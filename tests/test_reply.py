import email
import email.policy

import pytest
from a2a.types.a2a_pb2 import Artifact, Message, Part, Role, Task, TaskState, TaskStatus
from conftest import AUTHOR, BOT

from cf_email_unwrapper import replier as replier_module
from cf_email_unwrapper.replier import Replier
from cf_email_unwrapper.state import Mail, State


def submit(state: State, task_id="task-1", reply_cert=None, quote=None) -> None:
    """Record the mail the unwrapper handed to the agent as ``task_id``."""
    message_id = f"<{task_id}@example.com>" if task_id != "task-1" else "<parent@example.com>"
    state.record_mail(Mail(
        message_id=message_id,
        author=AUTHOR,
        subject="Grüße aus Berlin",
        references="<root@example.com> <mid@example.com>",
        reply_cert_pem=reply_cert,
        quote=quote,
        date="Fri, 30 Oct 2026 18:10:00 +0100",
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


class Outbox:
    """The replies queued in the state, plus how often the sender was woken."""

    def __init__(self, state: State):
        self.state = state
        self.wakeups = 0

    def wake(self):
        self.wakeups += 1

    @property
    def pushed(self):
        return self.state.outbox()


@pytest.fixture
def state():
    state = State(":memory:")
    yield state
    state.close()


@pytest.fixture
def outbox(state):
    return Outbox(state)


@pytest.fixture
def replier(pki, outbox, state):
    return Replier(smime=pki.smime(), agent_address=BOT, state=state, on_queued=outbox.wake)


def parse(reply):
    return reply.raw, email.message_from_bytes(reply.raw, policy=email.policy.default)


async def test_completed_reply_is_encrypted_signed_and_threaded(pki, replier, outbox, state):
    submit(state, reply_cert=pki.author_enc.cert_pem)
    task = make_task(TaskState.TASK_STATE_COMPLETED, artifact_text="Die Antwort")
    await replier.reply_to_task(task)

    [body] = outbox.pushed
    assert body.recipient == AUTHOR and outbox.wakeups == 1
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
    body = email.message_from_bytes(content, policy=email.policy.default)
    assert body.get_content_type() == "multipart/alternative"
    assert body.get_body(("plain",)).get_content().strip() == "Die Antwort"


async def test_markdown_is_rendered_to_html_and_raw_html_is_escaped(pki, replier, outbox, state):
    submit(state, reply_cert=pki.author_enc.cert_pem)
    markdown = "# Plan\n\n- **one**\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n<script>x()</script>"
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_COMPLETED, artifact_text=markdown))
    raw, _ = parse(outbox.pushed[0])
    body = email.message_from_bytes(pki.open_reply(raw, recipient=pki.author_enc),
                                    policy=email.policy.default)
    assert body.get_body(("plain",)).get_content().replace("\r\n", "\n").strip() == markdown
    html = body.get_body(("html",)).get_content()
    assert "<h1>Plan</h1>" in html and "<strong>one</strong>" in html and "<td>1</td>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


async def test_reply_signature_carries_the_bot_encryption_certificate(pki, replier, outbox, state):
    submit(state, reply_cert=pki.author_enc_rsa.cert_pem)
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_COMPLETED, agent_text="Hi"))
    raw, _ = parse(outbox.pushed[0])
    carried = pki.reply_certs(raw, pki.author_enc_rsa)
    assert "".join(pki.bot_enc.cert_pem.split()) in {"".join(c.split()) for c in carried}


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


async def test_oversized_answer_becomes_an_urgent_notice(replier, outbox, state, monkeypatch):
    monkeypatch.setattr(replier_module, "MAX_REPLY_BYTES", 50_000)
    submit(state)
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_COMPLETED, agent_text="x" * 200_000))
    [body] = outbox.pushed
    assert len(body.raw) < 50_000
    assert parse(body)[1]["Importance"] == "high"


async def test_reply_quotes_the_authors_mail_until_the_final_answer(pki, replier, outbox, state):
    submit(state, reply_cert=pki.author_enc.cert_pem, quote="Which city?\n<b>now</b>")
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_INPUT_REQUIRED, agent_text="Berlin?"))
    await replier.reply_to_task(make_task(TaskState.TASK_STATE_COMPLETED, agent_text="Done."))
    first, final = (email.message_from_bytes(pki.open_reply(r.raw, recipient=pki.author_enc),
                                             policy=email.policy.default) for r in outbox.pushed)
    for body in (first, final):
        plain = body.get_body(("plain",)).get_content().replace("\r\n", "\n")
        assert plain.endswith(f"On Fri, 30 Oct 2026 at 18:10, {AUTHOR} wrote:\n\n"
                              "> Which city?\n> <b>now</b>\n")
        html = body.get_body(("html",)).get_content().replace("\r\n", "\n")
        assert '<blockquote type="cite">Which city?<br>\n&lt;b&gt;now&lt;/b&gt;</blockquote>' in html
    # The decrypted text is not kept once the task has its final answer.
    assert state._db.execute("SELECT quote FROM mails").fetchone() == (None,)
