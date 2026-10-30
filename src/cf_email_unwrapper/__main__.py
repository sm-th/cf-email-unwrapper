"""Entry point: ``python -m cf_email_unwrapper``."""

import asyncio
import contextlib
import logging
import os
import sys

import uvicorn

from .agent import AgentClient, EchoAgent
from .config import Config, load_env
from .gateway import Gateway
from .outbox import Sender
from .push import create_app
from .replier import Replier
from .smime import Smime
from .state import State
from .worker import Worker

log = logging.getLogger(__name__)


async def main(cfg: Config) -> None:
    smime = Smime(
        openssl=cfg.openssl,
        signing_cert_pem=cfg.bot_signing_cert_pem,
        signing_key_pem=cfg.bot_signing_key_pem,
        encryption_cert_pem=cfg.bot_encryption_cert_pem,
        encryption_key_pem=cfg.bot_encryption_key_pem,
        ca_pem=cfg.ca_cert_pem,
    )
    if cfg.a2a_url:
        agent = AgentClient(cfg.a2a_url, cfg.a2a_token)
    else:
        agent = EchoAgent()
        log.warning("A2A_URL is not set: demo mode, every authenticated mail gets an echo reply")
    state = State(cfg.state_path)
    sender = Sender(state)
    replier = Replier(smime=smime, agent_address=cfg.agent_address, state=state,
                      on_queued=sender.wake)
    worker = Worker(config=cfg, smime=smime, agent=agent, replier=replier, state=state)
    gateway = Gateway(cfg.mailbox_url, lambda: smime.sign_mailbox_challenge(cfg.agent_address),
                      on_mail=worker.handle, on_connect=sender.wake)
    tasks = [
        asyncio.create_task(worker.recover()),
        asyncio.create_task(gateway.run()),
        asyncio.create_task(sender.run(gateway)),
    ]
    try:
        if cfg.a2a_url:
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(token=cfg.push_token, agent=agent, replier=replier),
                    host=cfg.push_host,
                    port=cfg.push_port,
                    log_level="info",
                )
            )
            # uvicorn handles SIGINT/SIGTERM; stop everything else when the server shuts down.
            await server.serve()
        else:
            # Demo mode has no push endpoint; Ctrl-C cancels this wait.
            await asyncio.gather(*tasks[1:])
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state.close()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help")):
        sys.exit("usage: cf-email-unwrapper [NAME | PATH.env]")
    try:
        cfg = Config.from_env(load_env(sys.argv[1] if len(sys.argv) == 2 else None,
                                       dict(os.environ)))
    except ValueError as e:
        sys.exit(f"cf-email-unwrapper: {e}")
    asyncio.run(main(cfg))


if __name__ == "__main__":
    run()
