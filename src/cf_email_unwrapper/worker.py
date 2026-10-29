"""Inbound loop: queue -> R2 -> S/MIME -> A2A SendMessage."""

import asyncio
import email
import email.policy
import logging
from datetime import UTC, datetime, timedelta

from .agent import AgentClient
from .cloudflare import PulledMessage, Queues, decode_body
from .config import Config
from .mail import a2a_message_id, context_id, extract_text, own_message_id, thread_root
from .queue_name import queue_name
from .r2 import ObjectStore
from .replier import REPLY_STATES, Replier
from .smime import Rejected, Smime

log = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 30


class Worker:
    def __init__(
        self,
        *,
        config: Config,
        queues: Queues,
        store: ObjectStore,
        smime: Smime,
        agent: AgentClient,
        replier: Replier,
    ):
        self._cfg = config
        self._queues = queues
        self._store = store
        self._smime = smime
        self._agent = agent
        self._replier = replier

    async def run(self) -> None:
        queue_id = await self.wait_for_queue()
        while True:
            try:
                handled = await self.poll_once(queue_id)
            except Exception:
                log.exception("polling failed")
                handled = 0
            if not handled:
                await asyncio.sleep(self._cfg.poll_interval)

    async def wait_for_queue(self) -> str:
        """The gateway creates the inbound queue on the first email; wait for it."""
        name = queue_name(self._cfg.agent_address)
        logged = False
        while True:
            try:
                queue_id = await self._queues.find_queue_id(name)
            except Exception:
                log.exception("looking up queue %s failed", name)
            else:
                if queue_id:
                    log.info("consuming queue %s (%s)", name, queue_id)
                    return queue_id
                if not logged:
                    log.info("queue %s does not exist yet; waiting for the first email", name)
                    logged = True
            await asyncio.sleep(self._cfg.poll_interval)

    async def poll_once(self, queue_id: str) -> int:
        messages = await self._queues.pull(queue_id)
        for message in messages:
            await self.handle(queue_id, message)
        return len(messages)

    async def handle(self, queue_id: str, message: PulledMessage) -> None:
        try:
            body = decode_body(message.body)
            if body.get("version") != 1:
                raise ValueError(f"unsupported message version {body.get('version')!r}")
            key = body["r2Key"]
            raw = await asyncio.to_thread(self._store.get, key)
            if raw is None:
                log.warning("R2 object %s is gone (already handled); dropping message", key)
                await self._queues.ack(queue_id, message.lease_id)
                return
            try:
                await self._deliver(raw)
            except Rejected as e:
                log.warning("rejected %s (envelope from %s): %s", key, body.get("from"), e)
            await self._queues.ack(queue_id, message.lease_id)
            await asyncio.to_thread(self._store.delete, key)
        except Exception:
            log.exception("handling queue message %s failed; retrying later", message.id)
            await self._queues.retry(queue_id, message.lease_id, RETRY_DELAY_SECONDS)

    async def _deliver(self, raw: bytes) -> None:
        opened = await asyncio.to_thread(
            self._smime.open,
            raw,
            allowed_senders=self._cfg.allowed_senders,
            max_age=timedelta(seconds=self._cfg.max_signature_age),
            now=datetime.now(UTC),
        )
        outer = email.message_from_bytes(raw, policy=email.policy.default)
        message_id = own_message_id(outer, raw)
        meta = {
            "author": opened.sender,
            "agentAddress": self._cfg.agent_address,
            "subject": str(outer.get("Subject", "")),
            "messageId": message_id,
            "references": str(outer.get("References", "")),
        }
        if opened.reply_cert_pem:
            meta["replyCertPem"] = opened.reply_cert_pem

        response = await self._agent.send(
            text=extract_text(opened.content),
            message_id=a2a_message_id(message_id),
            context_id=context_id(thread_root(outer, message_id)),
            metadata={"email": meta},
            push_url=self._cfg.push_url,
            push_token=self._cfg.push_token,
        )
        if response.HasField("task"):
            log.info("agent accepted %s as task %s", message_id, response.task.id)
            # A task that is already final will not necessarily trigger a notification.
            if response.task.status.state in REPLY_STATES:
                try:
                    await self._replier.reply_to_task(response.task)
                except Exception:
                    # The task is accepted; a push notification can still produce the reply.
                    log.exception("immediate reply for task %s failed", response.task.id)
        elif response.HasField("message"):
            await self._replier.reply_to_message(response.message, meta)
        else:
            raise RuntimeError("SendMessage returned neither a task nor a message")
