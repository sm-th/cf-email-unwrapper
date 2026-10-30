"""A2A v1.0 JSON-RPC client for the agent, built on the a2a-sdk transport."""

import httpx
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types.a2a_pb2 import (
    AgentCard,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    SendMessageResponse,
    Task,
    TaskPushNotificationConfig,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, VERSION_HEADER


class AgentClient:
    def __init__(self, url: str, token: str, http: httpx.AsyncClient | None = None):
        http = http or httpx.AsyncClient(timeout=60)
        http.headers["Authorization"] = f"Bearer {token}"
        http.headers.setdefault(VERSION_HEADER, PROTOCOL_VERSION_CURRENT)
        # The endpoint is configured directly, so no agent card discovery is needed.
        self._transport = JsonRpcTransport(http, AgentCard(), url)

    async def send(
        self,
        *,
        text: str,
        message_id: str,
        context_id: str,
        push_url: str | None,
        push_token: str | None,
    ) -> SendMessageResponse:
        message = Message(
            message_id=message_id,
            context_id=context_id,
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
        )
        request = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(
                return_immediately=True,
                task_push_notification_config=TaskPushNotificationConfig(
                    url=push_url, token=push_token
                ),
            ),
        )
        return await self._transport.send_message(request)

    async def get_task(self, task_id: str) -> Task:
        return await self._transport.get_task(GetTaskRequest(id=task_id))


class EchoAgent:
    """Demo mode stand-in for an agent: prints each message and answers ``Echo: <text>``."""

    async def send(
        self,
        *,
        text: str,
        message_id: str,
        context_id: str,
        push_url: str | None,
        push_token: str | None,
    ) -> SendMessageResponse:
        print(f"\n--- demo mode: message {message_id} ---\n{text}\n---", flush=True)
        reply = Message(
            message_id=f"echo-{message_id}",
            context_id=context_id,
            role=Role.ROLE_AGENT,
            parts=[Part(text=f"Echo: {text}")],
        )
        return SendMessageResponse(message=reply)

    async def get_task(self, task_id: str) -> Task:
        raise RuntimeError(f"demo mode has no agent to ask about task {task_id}")
