"""Turns finished agent tasks into signed (and encrypted) email replies on the outbox queue."""

import asyncio
import base64
import json
import logging
from typing import Any

from a2a.types.a2a_pb2 import Message, Task, TaskState
from google.protobuf.json_format import MessageToDict

from .cloudflare import CloudflareError, Queues
from .mail import compose_reply, text_entity
from .smime import Smime

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


def email_metadata(task: Task) -> dict[str, Any] | None:
    """The ``email`` metadata of the newest user message in the task history."""
    for message in reversed(task.history):
        meta = MessageToDict(message.metadata).get("email")
        if meta:
            return meta
    return MessageToDict(task.metadata).get("email") or None


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
    def __init__(self, *, queues: Queues, outbox_queue: str, smime: Smime, agent_address: str):
        self._queues = queues
        self._outbox_queue = outbox_queue
        self._outbox_id: str | None = None
        self._smime = smime
        self._agent_address = agent_address
        self._replied: set[tuple[str, int]] = set()
        self._lock = asyncio.Lock()

    async def reply_to_task(self, task: Task) -> None:
        state = task.status.state
        if state not in REPLY_STATES:
            return
        key = (task.id, state)
        async with self._lock:
            if key in self._replied:
                log.info("task %s already answered in state %s", task.id, TaskState.Name(state))
                return
            meta = email_metadata(task)
            if meta is None:
                log.error("task %s has no email metadata in its history; cannot reply", task.id)
                return
            text = agent_text(task) or (
                f"The agent finished without a text answer ({TaskState.Name(state)})."
            )
            await self.reply(meta, text, urgent=REPLY_STATES[state])
            self._replied.add(key)

    async def reply_to_message(self, message: Message, meta: dict[str, Any]) -> None:
        """Reply for an agent that answered SendMessage with a direct message."""
        await self.reply(meta, _message_text(message), urgent=False)

    async def reply(self, meta: dict[str, Any], text: str, *, urgent: bool) -> None:
        body = await asyncio.to_thread(self._outbox_body, meta, text, urgent)
        if len(json.dumps(body)) > MAX_OUTBOX_JSON:
            log.warning("reply to %s is too large for the outbox; sending a notice", meta["author"])
            notice = (
                "The agent's answer is too large to be delivered by email "
                f"({len(text.encode())} bytes of text)."
            )
            body = await asyncio.to_thread(self._outbox_body, meta, notice, True)
        await self._queues.push(await self._outbox_queue_id(), body)
        log.info("reply to %s queued (%s)", meta["author"], meta.get("messageId"))

    def _outbox_body(self, meta: dict[str, Any], text: str, urgent: bool) -> dict[str, Any]:
        sealed = self._smime.seal(text_entity(text), meta.get("replyCertPem"))
        raw = compose_reply(
            sealed,
            agent_address=self._agent_address,
            author=meta["author"],
            subject=meta.get("subject", ""),
            parent_message_id=meta["messageId"],
            parent_references=meta.get("references", ""),
            urgent=urgent,
        )
        return {
            "version": 1,
            "from": self._agent_address,
            "to": meta["author"],
            "raw": base64.b64encode(raw).decode(),
        }

    async def _outbox_queue_id(self) -> str:
        if self._outbox_id is None:
            self._outbox_id = await self._queues.find_queue_id(self._outbox_queue)
            if self._outbox_id is None:
                raise CloudflareError(f"outbox queue {self._outbox_queue!r} does not exist")
        return self._outbox_id
