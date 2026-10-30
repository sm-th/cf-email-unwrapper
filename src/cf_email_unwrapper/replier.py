"""Turns finished agent tasks into signed (and encrypted) email replies in the local outbox."""

import asyncio
import logging
from collections.abc import Callable

from a2a.types.a2a_pb2 import Message, Task, TaskState

from .mail import compose_reply, text_entity
from .smime import Smime
from .state import Mail, State

log = logging.getLogger(__name__)

# Task states that produce a reply, mapped to "urgent" (failure) priority.
REPLY_STATES: dict[int, bool] = {
    TaskState.TASK_STATE_COMPLETED: False,
    TaskState.TASK_STATE_INPUT_REQUIRED: False,
    TaskState.TASK_STATE_FAILED: True,
    TaskState.TASK_STATE_REJECTED: True,
    TaskState.TASK_STATE_CANCELED: True,
}

# Larger replies are replaced by a notice; Cloudflare send_email accepts up to 25 MiB.
MAX_REPLY_BYTES = 20_000_000

# Input-required can still be followed by another state; the others end the task.
NON_FINAL_REPLY_STATES = {TaskState.TASK_STATE_INPUT_REQUIRED}


def _message_text(message: Message) -> str:
    return "\n".join(p.text for p in message.parts if p.text).strip()


def agent_text(task: Task) -> str:
    texts: list[str] = []
    for artifact in task.artifacts:
        text = "\n".join(p.text for p in artifact.parts if p.text).strip()
        if text:
            texts.append(text)
    if task.status.HasField("message"):
        text = _message_text(task.status.message)
        if text and text not in texts:
            texts.append(text)
    return "\n\n".join(texts)


class Replier:
    def __init__(
        self,
        *,
        smime: Smime,
        agent_address: str,
        state: State,
        on_queued: Callable[[], None] = lambda: None,
    ):
        self._smime = smime
        self._agent_address = agent_address
        self._state = state
        self._on_queued = on_queued
        self._lock = asyncio.Lock()

    async def reply_to_task(self, task: Task) -> None:
        state = task.status.state
        if state not in REPLY_STATES:
            return
        async with self._lock:
            if self._state.replied(task.id, state):
                log.info("task %s already answered in state %s", task.id, TaskState.Name(state))
                return
            mail = self._state.mail_for_task(task.id)
            if mail is None:
                log.error("task %s was not submitted by this unwrapper; cannot reply", task.id)
                return
            text = agent_text(task) or (
                f"The agent finished without a text answer ({TaskState.Name(state)})."
            )
            raw = await self.compose(mail, text, urgent=REPLY_STATES[state])
            self._state.record_reply(
                task.id, state, final=state not in NON_FINAL_REPLY_STATES,
                recipient=mail.author, raw=raw,
            )
        self._queued(mail)

    async def reply_to_message(self, message: Message, mail: Mail) -> None:
        """Reply for an agent that answered SendMessage with a direct message."""
        raw = await self.compose(mail, _message_text(message), urgent=False)
        self._state.record_direct_reply(mail.message_id, recipient=mail.author, raw=raw)
        self._queued(mail)

    def _queued(self, mail: Mail) -> None:
        log.info("reply to %s queued (%s)", mail.author, mail.message_id)
        self._on_queued()

    async def compose(self, mail: Mail, text: str, *, urgent: bool) -> bytes:
        raw = await asyncio.to_thread(self._compose, mail, text, urgent)
        if len(raw) > MAX_REPLY_BYTES:
            log.warning("reply to %s is too large to send; sending a notice", mail.author)
            notice = (
                "The agent's answer is too large to be delivered by email "
                f"({len(text.encode())} bytes of text)."
            )
            raw = await asyncio.to_thread(self._compose, mail, notice, True)
        return raw

    def _compose(self, mail: Mail, text: str, urgent: bool) -> bytes:
        sealed = self._smime.seal(text_entity(text), mail.reply_cert_pem)
        return compose_reply(
            sealed,
            agent_address=self._agent_address,
            author=mail.author,
            subject=mail.subject,
            parent_message_id=mail.message_id,
            parent_references=mail.references,
            urgent=urgent,
        )
