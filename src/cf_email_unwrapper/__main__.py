"""Entry point: ``python -m cf_email_unwrapper``."""

import asyncio
import contextlib
import logging

import uvicorn

from .agent import AgentClient
from .config import Config
from .gateway import Gateway
from .outbox import Sender
from .push import create_app
from .replier import Replier
from .smime import Smime
from .state import State
from .worker import Worker


async def main() -> None:
    cfg = Config.from_env()
    smime = Smime(
        openssl=cfg.openssl,
        signing_cert_pem=cfg.bot_signing_cert_pem,
        signing_key_pem=cfg.bot_signing_key_pem,
        encryption_cert_pem=cfg.bot_encryption_cert_pem,
        encryption_key_pem=cfg.bot_encryption_key_pem,
        ca_pem=cfg.ca_cert_pem,
    )
    agent = AgentClient(cfg.a2a_url, cfg.a2a_token)
    state = State(cfg.state_path)
    sender = Sender(state)
    replier = Replier(smime=smime, agent_address=cfg.agent_address, state=state,
                      on_queued=sender.wake)
    worker = Worker(config=cfg, smime=smime, agent=agent, replier=replier, state=state)
    gateway = Gateway(cfg.mailbox_url, lambda: smime.sign_mailbox_challenge(cfg.agent_address),
                      on_mail=worker.handle, on_connect=sender.wake)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(token=cfg.push_token, agent=agent, replier=replier),
            host=cfg.push_host,
            port=cfg.push_port,
            log_level="info",
        )
    )
    # uvicorn handles SIGINT/SIGTERM; stop everything else when the server shuts down.
    tasks = [
        asyncio.create_task(worker.recover()),
        asyncio.create_task(gateway.run()),
        asyncio.create_task(sender.run(gateway)),
    ]
    try:
        await server.serve()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state.close()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
