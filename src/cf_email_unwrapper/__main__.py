"""Entry point: ``python -m cf_email_unwrapper``."""

import asyncio
import contextlib
import logging

import uvicorn

from .agent import AgentClient
from .cloudflare import Queues
from .config import Config
from .push import create_app
from .r2 import R2Store
from .replier import Replier
from .smime import Smime
from .state import State
from .worker import Worker


async def main() -> None:
    cfg = Config.from_env()
    queues = Queues(cfg.cf_account_id, cfg.cf_api_token)
    smime = Smime(
        openssl=cfg.openssl,
        bot_cert_pem=cfg.bot_cert_pem,
        bot_key_pem=cfg.bot_key_pem,
        ca_pem=cfg.ca_cert_pem,
    )
    agent = AgentClient(cfg.a2a_url, cfg.a2a_token)
    state = State(cfg.state_path)
    replier = Replier(
        queues=queues,
        outbox_queue=cfg.outbox_queue,
        smime=smime,
        agent_address=cfg.agent_address,
        state=state,
    )
    store = R2Store(
        endpoint=cfg.r2_endpoint,
        access_key_id=cfg.r2_access_key_id,
        secret_access_key=cfg.r2_secret_access_key,
        bucket=cfg.r2_bucket,
    )
    worker = Worker(
        config=cfg,
        queues=queues,
        store=store,
        smime=smime,
        agent=agent,
        replier=replier,
        state=state,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(token=cfg.push_token, agent=agent, replier=replier),
            host=cfg.push_host,
            port=cfg.push_port,
            log_level="info",
        )
    )
    # uvicorn handles SIGINT/SIGTERM; stop the worker when the server shuts down.
    worker_task = asyncio.create_task(worker.run())
    try:
        await server.serve()
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        state.close()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
