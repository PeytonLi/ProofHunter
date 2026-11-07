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

from uagents.crypto import Identity

from proofhunter.acp.agent import build_agent, default_service_factory
from proofhunter.acp.client import create_control_client
from proofhunter.acp.registration import register_mailbox, registration_key
from proofhunter.acp.service import ChatService, MemoryKeyValue, ReplyLedger
from proofhunter.acp.settings import (
    load_acp_settings,
    prepare_storage,
    require_publication_approval,
)
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
    parser.add_argument("command", nargs="?", choices=["register"],
                        help="connect the mailbox once using AGENTVERSE_API_KEY")
    parser.add_argument("--local", action="store_true",
                        help="chat over stdin/stdout instead of starting the agent")
    parser.add_argument("--address", action="store_true",
                        help="print the seed-derived public address without connecting")
    args = parser.parse_args(argv)
    if args.command and (args.local or args.address):
        parser.error("register cannot be combined with --local or --address")
    try:
        settings = load_acp_settings()
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
    if args.address:
        print(Identity.from_seed(seed.get_secret_value(), 0).address)
        return 0
    try:
        require_publication_approval()
        key = registration_key() if args.command == "register" else None
        prepare_storage()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    agent = build_agent(seed.get_secret_value(), default_service_factory(client),
                        port=settings.agent.port, mailbox=settings.agent.mailbox,
                        publish_agent_details=True)
    if key is not None:
        success = asyncio.run(register_mailbox(agent, key))
        print("registration succeeded" if success else
              "registration failed: account connection unavailable")
        return 0 if success else 1
    agent.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
