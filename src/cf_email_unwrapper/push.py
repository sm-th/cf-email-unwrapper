"""HTTP endpoint for A2A push notifications."""

import hmac
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from .agent import AgentClient
from .replier import Replier

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-A2A-Notification-Token"
PUSH_PATH = "/a2a/push"


def notification_task_id(payload: Any) -> str | None:
    """Task id from a StreamResponse-shaped notification (or a bare Task)."""
    if not isinstance(payload, dict):
        return None
    for key, field in (("task", "id"), ("statusUpdate", "taskId"), ("artifactUpdate", "taskId")):
        inner = payload.get(key)
        if isinstance(inner, dict) and inner.get(field):
            return inner[field]
    return payload.get("id") or payload.get("taskId")


def create_app(*, token: str, agent: AgentClient, replier: Replier) -> Starlette:
    async def push(request: Request) -> PlainTextResponse:
        received = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(received.encode(), token.encode()):
            return PlainTextResponse("invalid notification token", status_code=401)
        try:
            task_id = notification_task_id(await request.json())
        except ValueError:
            task_id = None
        if not task_id:
            return PlainTextResponse("no task id", status_code=400)
        try:
            task = await agent.get_task(task_id)
            await replier.reply_to_task(task)
        except Exception:
            log.exception("handling notification for task %s failed", task_id)
            return PlainTextResponse("reply failed", status_code=502)
        return PlainTextResponse("ok")

    return Starlette(routes=[Route(PUSH_PATH, push, methods=["POST"])])
