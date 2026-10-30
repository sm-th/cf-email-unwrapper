"""Stand-in A2A agent for testing a guardian: prints each message and answers ``Echo: <text>``.

Run with ``cf-email-echo-agent`` (``nix run .#echo-agent``); listens on 127.0.0.1:$ECHO_PORT
(default 8800), so point the guardian at ``A2A_URL=http://127.0.0.1:8800/``. It accepts any
bearer token and answers through push notifications, like a real agent.
"""

import asyncio
import logging
import os

import httpx
import uvicorn
from a2a.helpers.proto_helpers import new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import (
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
    TaskUpdater,
)
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    TaskState,
)
from starlette.applications import Starlette

class Echo(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(context.task_id, context.context_id, TaskState.TASK_STATE_SUBMITTED,
                         history=[context.message])
            )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        text = context.get_user_input()
        print(f"\n--- message (task {context.task_id}, context {context.context_id}) ---\n"
              f"{text}\n---", flush=True)
        await asyncio.sleep(2)
        await updater.add_artifact([Part(text=f"Echo: {text}")])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def create_app(port: int) -> Starlette:
    card = AgentCard(
        name="echo",
        description="Prints each message and answers with its text",
        version="1",
        supported_interfaces=[
            AgentInterface(url=f"http://127.0.0.1:{port}/", protocol_binding="JSONRPC",
                           protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(id="echo", name="echo", description="Echo the text", tags=["test"])],
    )
    configs = InMemoryPushNotificationConfigStore()
    handler = DefaultRequestHandler(
        agent_executor=Echo(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=configs,
        push_sender=BasePushNotificationSender(httpx.AsyncClient(), configs),
    )
    return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))


def run() -> None:
    # Only warnings from the libraries; the messages themselves are printed.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("ECHO_PORT", "8800"))
    print(f"echo agent on http://127.0.0.1:{port}/ (A2A_URL for the guardian)", flush=True)
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    run()
