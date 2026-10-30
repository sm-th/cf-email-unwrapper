"""Sends the replies queued in the state outbox through the gateway."""

import asyncio
import logging
import time

from .gateway import Gateway
from .state import State

log = logging.getLogger(__name__)


class Sender:
    def __init__(
        self,
        state: State,
        *,
        retry_delay: float = 30,
        max_retry_delay: float = 3600,
        max_age: float = 86400,
    ):
        self._state = state
        self._retry_delay = retry_delay
        self._max_retry_delay = max_retry_delay
        self._max_age = max_age
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """A reply was queued or the gateway (re)connected."""
        self._wake.set()

    async def run(self, gateway: Gateway) -> None:
        while True:
            self._wake.clear()
            delay = await self.send_due(gateway) if gateway.connected else None
            try:
                async with asyncio.timeout(delay):
                    await self._wake.wait()
            except TimeoutError:
                pass

    async def send_due(self, gateway: Gateway) -> float | None:
        """Send every due reply; return the seconds until the next one is due (None: none left)."""
        for reply in self._state.outbox():
            now = time.time()
            if reply.next_attempt_at > now:
                break
            try:
                await gateway.send(reply.id, reply.recipient, reply.raw)
            except Exception as e:
                if not gateway.connected:
                    log.warning("reply %s to %s not sent: %s", reply.id, reply.recipient, e)
                    return None  # tried again after the reconnect
                if now - reply.created_at > self._max_age:
                    log.error("dropping reply %s to %s after %d attempts: %s",
                              reply.id, reply.recipient, reply.attempts + 1, e)
                    self._state.remove_reply(reply.id)
                    continue
                delay = min(self._retry_delay * 2**reply.attempts, self._max_retry_delay)
                log.warning("reply %s to %s failed (%s); retrying in %g s",
                            reply.id, reply.recipient, e, delay)
                self._state.postpone_reply(reply.id, now + delay)
            else:
                self._state.remove_reply(reply.id)
                log.info("reply %s to %s sent", reply.id, reply.recipient)
        due = self._state.next_reply_due()
        return None if due is None else max(due - time.time(), 0)
