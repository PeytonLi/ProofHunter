"""``python -m proofhunter.acp``: the separate ACP chat process.

Live profile: starts the uAgents mailbox agent (needs ``AGENT_SEED_PHRASE`` plus the
control API base and token). Offline profile: never registers or publishes an
agent; ``--local`` runs the same chat handling over stdin/stdout against the
control API at ``PROOFHUNTER_API_BASE``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from proofhunter.acp.agent import build_agent, default_service_factory
from proofhunter.acp.client import create_control_client
from proofhunter.acp.service import ChatService, MemoryKeyValue, ReplyLedger
from proofhunter.config import load_settings
from proofhunter.ports import ConfigError


async def _local_chat(service: ChatService) -> int:
    for number, line in enumerate(sys.stdin, start=1):
        if not line.strip():
            continue
        print(await service.reply_for("local-cli", f"local-{number}", line.rstrip("\n")),
              flush=True)
        print("---", flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m proofhunter.acp", description=__doc__)
    parser.add_argument("--local", action="store_true",
                        help="chat over stdin/stdout instead of starting the agent")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        client = create_control_client(settings)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    if args.local:
        return asyncio.run(_local_chat(ChatService(client, ReplyLedger(MemoryKeyValue()))))
    if settings.offline:
        print("offline profile: the public ACP agent is not started. Use --local to chat "
              "against the offline control API.", file=sys.stderr)
        return 2
    seed = settings.agent.seed_phrase
    if seed is None:
        print("configuration error: AGENT_SEED_PHRASE is required", file=sys.stderr)
        return 2
    agent = build_agent(seed.get_secret_value(), default_service_factory(client),
                        port=settings.agent.port, mailbox=settings.agent.mailbox)
    agent.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
