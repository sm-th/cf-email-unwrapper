"""Inbound path: gateway mail frame -> S/MIME -> A2A SendMessage."""

import asyncio
import email
import email.policy
import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from .agent import AgentClient
from .config import Config
from .gateway import InboundMail
from .mail import (
    QuoteTrim,
    a2a_message_id,
    context_id,
    extract_text,
    message_ids,
    own_message_id,
    thread_root,
    trim_quotes,
)
from .replier import REPLY_STATES, Replier
from .smime import Opened, Rejected, Smime
from .state import Mail, State

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        *,
        config: Config,
        smime: Smime,
        agent: AgentClient,
        replier: Replier,
        state: State,
        retry_delay: float = 30,
    ):
        self._cfg = config
        self._smime = smime
        self._agent = agent
        self._replier = replier
        self._state = state
        self._retry_delay = retry_delay
        # Serializes the Message-ID check and SendMessage, so duplicates reach the agent once.
        self._deliver_lock = asyncio.Lock()

    async def recover(self) -> None:
        """Answer tasks that finished while no notification could reach this process."""
        for task_id in self._state.open_tasks():
            try:
                await self._replier.reply_to_task(await self._agent.get_task(task_id))
            except Exception:
                log.exception("checking open task %s failed", task_id)

    async def handle(self, mail: InboundMail) -> bool:
        """Deliver a gateway mail, retrying transient failures; True once it may be acked."""
        while True:
            try:
                await self._deliver(mail.raw)
                return True
            except Rejected as e:
                log.warning("rejected mail %s (envelope from %s): %s", mail.id, mail.sender, e)
                return True
            except Exception:
                log.exception("handling mail %s failed; retrying in %g s", mail.id,
                              self._retry_delay)
            await asyncio.sleep(self._retry_delay)

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
        async with self._deliver_lock:
            await self._send_to_agent(outer, message_id, opened)

    async def _send_to_agent(self, outer: EmailMessage, message_id: str, opened: Opened) -> None:
        if self._state.delivered(message_id):
            log.info("%s was already handed to the agent; skipping the redelivery", message_id)
            return
        context = context_id(thread_root(outer, message_id))
        text = extract_text(opened.content)
        mail = Mail(
            message_id=message_id,
            author=opened.sender,
            subject=str(outer.get("Subject", "")),
            references=str(outer.get("References", "")),
            reply_cert_pem=opened.reply_cert_pem,
            context_id=context,
            quote=trim_quotes(text, QuoteTrim.FULL),
            date=str(outer.get("Date", "")) or None,
        )
        # Which mail this one answers: one of our replies, or a mail of the author we delivered.
        in_reply_to = message_ids(outer.get("In-Reply-To"))
        parent = self._state.parent(in_reply_to[0]) if in_reply_to else None
        self._state.record_mail(mail)

        # The agent already knows its latest reply; any other quoted mail tells it what is answered.
        mode = QuoteTrim.FULL if parent and parent.latest_reply else QuoteTrim.ONE_LEVEL
        response = await self._agent.send(
            text=trim_quotes(text, mode),
            message_id=a2a_message_id(message_id),
            context_id=context,
            reference_task_ids=[parent.task_id] if parent and parent.task_id else [],
            push_url=self._cfg.push_url,
            push_token=self._cfg.push_token,
        )
        if response.HasField("task"):
            log.info("agent accepted %s as task %s", message_id, response.task.id)
            self._state.attach_task(message_id, response.task.id)
            # A task that is already final will not necessarily trigger a notification.
            if response.task.status.state in REPLY_STATES:
                try:
                    await self._replier.reply_to_task(response.task)
                except Exception:
                    # The task is accepted; a push notification can still produce the reply.
                    log.exception("immediate reply for task %s failed", response.task.id)
        elif response.HasField("message"):
            await self._replier.reply_to_message(response.message, mail)
        else:
            raise RuntimeError("SendMessage returned neither a task nor a message")
