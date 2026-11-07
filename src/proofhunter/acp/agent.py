"""Official uAgents Agent Chat Protocol wiring (PRD section 3, Fetch.ai).

Inbound ``ChatMessage``: ack its ``msg_id`` first, then send one text reply that
ends with ``EndSessionContent`` (one chat turn per request). Inbound
``ChatAcknowledgement``: log only, never reply, so acks cannot loop.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

from proofhunter.acp.service import ChatService, ReplyLedger

AGENT_NAME: Final = "proofhunter"
AGENT_DESCRIPTION: Final = (
    "ProofHunter verifies GitHub pull requests on enrolled repositories: it pins base and "
    "head commits, tests informative inputs and returns a verdict with a reproducible "
    "evidence receipt. Commands: verify <PR URL>, status <run_id>, evidence <run_id>, help."
)
AGENT_README: Final = Path(__file__).with_name("AGENT_README.md")
#: More content parts than this is malformed for a plain-text command.
MAX_CONTENT_PARTS: Final = 16


def reply_msg_id(inbound_msg_id: str) -> uuid.UUID:
    """Deterministic UUIDv4-shaped reply ID, so a redelivered request gets the same reply ID."""
    digest = hashlib.sha256(f"proofhunter-reply:{inbound_msg_id}".encode()).digest()
    return uuid.UUID(bytes=digest[:16], version=4)


def message_text(msg: Any) -> str:
    """Concatenated text parts of a chat message (non-text parts are ignored)."""
    content = list(getattr(msg, "content", []) or [])
    if len(content) > MAX_CONTENT_PARTS:
        return "\x00malformed"  # parsed as a control-character rejection
    return "".join(part.text for part in content if isinstance(part, TextContent)
                   and isinstance(part.text, str))


async def handle_chat_message(service: ChatService, ctx: Any, sender: str, msg: Any) -> None:
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id,
                                               timestamp=datetime.now(UTC)))
    inbound = str(msg.msg_id)
    text = await service.reply_for(sender, inbound, message_text(msg))
    await ctx.send(sender, ChatMessage(
        content=[TextContent(text=text), EndSessionContent()],
        msg_id=reply_msg_id(inbound),
        timestamp=datetime.now(UTC)))


async def handle_acknowledgement(ctx: Any, sender: str, msg: Any) -> None:
    logger = getattr(ctx, "logger", None)
    if logger is not None:
        logger.debug("chat ack received for %s", msg.acknowledged_msg_id)


def build_chat_protocol(service: ChatService) -> Any:
    protocol = Protocol(spec=chat_protocol_spec)

    @protocol.on_message(ChatMessage)  # type: ignore[untyped-decorator]
    async def _on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
        await handle_chat_message(service, ctx, sender, msg)

    @protocol.on_message(ChatAcknowledgement)  # type: ignore[untyped-decorator]
    async def _on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
        await handle_acknowledgement(ctx, sender, msg)

    return protocol


def build_agent(seed_phrase: str, service_factory: Any, *, port: int = 8001,
                mailbox: bool = True, publish_agent_details: bool = False) -> Any:
    """Create the agent and include the chat protocol with its manifest.

    ``service_factory(storage)`` builds the ``ChatService`` with the agent's persistent
    key-value storage, so message dedup survives restarts. Nothing is contacted until
    ``agent.run()``.
    """
    agent = Agent(name=AGENT_NAME, seed=seed_phrase, port=port, mailbox=mailbox,
                  description=AGENT_DESCRIPTION, readme_path=str(AGENT_README),
                  publish_agent_details=publish_agent_details)
    service = service_factory(agent.storage)
    agent.include(build_chat_protocol(service), publish_manifest=publish_agent_details)
    return agent


def default_service_factory(client: Any) -> Any:
    def factory(storage: Any) -> ChatService:
        return ChatService(client, ReplyLedger(storage))

    return factory


__all__ = [
    "AGENT_DESCRIPTION",
    "AGENT_NAME",
    "build_agent",
    "build_chat_protocol",
    "default_service_factory",
    "handle_acknowledgement",
    "handle_chat_message",
    "message_text",
    "reply_msg_id",
]
