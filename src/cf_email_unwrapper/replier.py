"""Turns finished agent tasks into signed (and encrypted) email replies on the outbox queue."""

import asyncio
import base64
import json
import logging
from typing import Any

from a2a.types.a2a_pb2 import Message, Task, TaskState

from .cloudflare import CloudflareError, Queues
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

# A queue message is limited to 128 KB; leave room for the envelope.
MAX_OUTBOX_JSON = 120_000

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
        self, *, queues: Queues, outbox_queue: str, smime: Smime, agent_address: str, state: State
    ):
        self._queues = queues
        self._outbox_queue = outbox_queue
        self._outbox_id: str | None = None
        self._smime = smime
        self._agent_address = agent_address
        self._state = state
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
            await self.reply(mail, text, urgent=REPLY_STATES[state])
            self._state.record_reply(task.id, state, final=state not in NON_FINAL_REPLY_STATES)

    async def reply_to_message(self, message: Message, mail: Mail) -> None:
        """Reply for an agent that answered SendMessage with a direct message."""
        await self.reply(mail, _message_text(message), urgent=False)
        self._state.close_mail(mail.message_id)

    async def reply(self, mail: Mail, text: str, *, urgent: bool) -> None:
        body = await asyncio.to_thread(self._outbox_body, mail, text, urgent)
        if len(json.dumps(body)) > MAX_OUTBOX_JSON:
            log.warning("reply to %s is too large for the outbox; sending a notice", mail.author)
            notice = (
                "The agent's answer is too large to be delivered by email "
                f"({len(text.encode())} bytes of text)."
            )
            body = await asyncio.to_thread(self._outbox_body, mail, notice, True)
        await self._queues.push(await self._outbox_queue_id(), body)
        log.info("reply to %s queued (%s)", mail.author, mail.message_id)

    def _outbox_body(self, mail: Mail, text: str, urgent: bool) -> dict[str, Any]:
        sealed = self._smime.seal(text_entity(text), mail.reply_cert_pem)
        raw = compose_reply(
            sealed,
            agent_address=self._agent_address,
            author=mail.author,
            subject=mail.subject,
            parent_message_id=mail.message_id,
            parent_references=mail.references,
            urgent=urgent,
        )
        return {
            "version": 1,
            "from": self._agent_address,
            "to": mail.author,
            "raw": base64.b64encode(raw).decode(),
        }

    async def _outbox_queue_id(self) -> str:
        if self._outbox_id is None:
            self._outbox_id = await self._queues.find_queue_id(self._outbox_queue)
            if self._outbox_id is None:
                raise CloudflareError(f"outbox queue {self._outbox_queue!r} does not exist")
        return self._outbox_id
