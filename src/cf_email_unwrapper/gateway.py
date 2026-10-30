"""WebSocket client for the cf-email-gateway mailbox (see README, "Gateway protocol").

Every connection attempt carries a fresh ``X-Mailbox-Signature``: a detached CMS signature of
the mailbox challenge made with the bot signing key. Binary frames are a UTF-8 JSON header, a
newline and raw bytes. The gateway pushes ``mail`` frames; the client answers ``ack`` (text)
once a mail is handled and sends replies as ``send`` frames, which the gateway confirms with
``sent`` or ``error``.
"""

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Mailbox-Signature"


class GatewayError(Exception):
    pass


@dataclass(frozen=True)
class InboundMail:
    id: str
    sender: str
    received_at: str
    raw: bytes


def encode_frame(header: dict[str, Any], body: bytes) -> bytes:
    return json.dumps(header).encode() + b"\n" + body


def decode_frame(data: bytes) -> tuple[dict[str, Any], bytes]:
    head, sep, body = data.partition(b"\n")
    header = json.loads(head)
    if not sep or not isinstance(header, dict):
        raise ValueError("binary frame without a JSON header line")
    return header, body


class Gateway:
    """One connection to the agent's mailbox, re-established with backoff.

    ``sign`` returns a fresh DER CMS signature of the mailbox challenge; it runs in a thread
    before every connection attempt. ``on_mail`` runs in its own task per mail and returns True
    when the mail may be acknowledged. ``on_connect`` is called after every successful
    (re)connection.
    """

    def __init__(
        self,
        url: str,
        sign: Callable[[], bytes],
        *,
        on_mail: Callable[[InboundMail], Awaitable[bool]],
        on_connect: Callable[[], None] = lambda: None,
        send_timeout: float = 60,
        ping_interval: float = 20,
        min_delay: float = 1,
        max_delay: float = 60,
    ):
        self._url = url
        self._sign = sign
        self._on_mail = on_mail
        self._on_connect = on_connect
        self._send_timeout = send_timeout
        self._ping_interval = ping_interval
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[None]] = {}
        # Mail ids being handled; the gateway resends unacked mail on every new connection.
        self._handling: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def run(self) -> None:
        delay = self._min_delay
        try:
            while True:
                try:
                    signature = await asyncio.to_thread(self._sign)
                    async with connect(
                        self._url,
                        additional_headers={SIGNATURE_HEADER: base64.b64encode(signature).decode()},
                        max_size=None,
                        ping_interval=self._ping_interval,
                    ) as ws:
                        log.info("connected to the gateway mailbox %s", self._url)
                        delay = self._min_delay
                        self._ws = ws
                        self._on_connect()
                        await self._receive(ws)
                    log.warning(
                        "gateway closed the connection (%s %s)", ws.close_code, ws.close_reason
                    )
                except ConnectionClosed as e:
                    log.warning("lost the gateway connection: %s", e)
                except (OSError, TimeoutError) as e:
                    log.warning("cannot connect to the gateway: %s", e)
                except Exception as e:  # e.g. InvalidStatus for a rejected signature
                    log.error("gateway connection failed: %s", e)
                finally:
                    self._disconnected()
                log.info("reconnecting in %g s", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_delay)
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _disconnected(self) -> None:
        self._ws = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(GatewayError("gateway connection lost"))

    async def _receive(self, ws: ClientConnection) -> None:
        async for message in ws:
            try:
                if isinstance(message, bytes):
                    header, body = decode_frame(message)
                    if header.get("type") != "mail":
                        raise ValueError(f"unexpected binary frame {header.get('type')!r}")
                    self._mail(header, body)
                else:
                    self._reply(json.loads(message))
            except (ValueError, KeyError, TypeError) as e:
                log.error("ignoring malformed gateway frame: %s", e)

    def _mail(self, header: dict[str, Any], raw: bytes) -> None:
        mail = InboundMail(
            id=str(header["id"]),
            sender=str(header.get("from", "")),
            received_at=str(header.get("receivedAt", "")),
            raw=raw,
        )
        if mail.id in self._handling:
            return  # resent after a reconnect while still being handled
        self._handling.add(mail.id)
        task = asyncio.create_task(self._handle(mail))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, mail: InboundMail) -> None:
        try:
            try:
                ack = await self._on_mail(mail)
            except Exception:
                log.exception("handling mail %s failed", mail.id)
                ack = False
            if ack:
                await self._ack(mail.id)
        finally:
            self._handling.discard(mail.id)

    async def _ack(self, mail_id: str) -> None:
        ws = self._ws
        try:
            if ws is None:
                raise GatewayError("not connected")
            await ws.send(json.dumps({"type": "ack", "id": mail_id}))
        except (GatewayError, ConnectionClosed):
            log.warning("could not ack mail %s; the gateway will resend it", mail_id)

    def _reply(self, frame: Any) -> None:
        kind, frame_id = frame.get("type"), frame.get("id")
        future = self._pending.get(frame_id) if isinstance(frame_id, str) else None
        if kind == "sent" and future and not future.done():
            future.set_result(None)
        elif kind == "error" and future and not future.done():
            future.set_exception(GatewayError(str(frame.get("message"))))
        elif kind == "error":
            log.error("gateway error for %s: %s", frame_id, frame.get("message"))
        else:
            log.warning("ignoring gateway frame %s for %s", kind, frame_id)

    async def send(self, id: str, to: str, raw: bytes) -> None:
        """Send ``raw`` to ``to`` through the gateway; raises GatewayError unless it reports sent."""
        ws = self._ws
        if ws is None:
            raise GatewayError("not connected")
        if id in self._pending:
            raise GatewayError(f"send {id} is already in flight")
        future = asyncio.get_running_loop().create_future()
        self._pending[id] = future
        try:
            try:
                await ws.send(encode_frame({"type": "send", "id": id, "to": to}, raw))
            except ConnectionClosed as e:
                raise GatewayError(f"gateway connection lost: {e}") from e
            try:
                async with asyncio.timeout(self._send_timeout):
                    await future
            except TimeoutError:
                raise GatewayError(f"no answer to send {id}") from None
        finally:
            del self._pending[id]
