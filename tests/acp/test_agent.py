from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from fixtures.adapters import FixtureControlApi
from proofhunter.acp.__main__ import main
from proofhunter.acp.agent import (
    MAX_CONTENT_PARTS,
    build_chat_protocol,
    handle_acknowledgement,
    handle_chat_message,
    message_text,
    reply_msg_id,
)
from proofhunter.acp.service import ChatService, MemoryKeyValue, ReplyLedger

from .conftest import PR_URL

SENDER = "agent1qfakesender000000000000000000000000000000000000000000000"


class FakeContext:
    """Records ``ctx.send`` like the uAgents dispatcher would deliver it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def send(self, destination: str, message: Any, **_: Any) -> None:
        self.sent.append((destination, message))


def chat(text: str, msg_id: uuid.UUID | None = None, extra: list[Any] | None = None
         ) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(UTC), msg_id=msg_id or uuid.uuid4(),
                       content=[*(extra or []), TextContent(text=text)])


def service() -> ChatService:
    return ChatService(FixtureControlApi(), ReplyLedger(MemoryKeyValue()))


async def test_ack_first_then_text_and_end_session() -> None:
    ctx, msg = FakeContext(), chat(f"verify {PR_URL}")
    await handle_chat_message(service(), ctx, SENDER, msg)
    assert [type(m) for _, m in ctx.sent] == [ChatAcknowledgement, ChatMessage]
    assert all(dest == SENDER for dest, _ in ctx.sent)
    ack, reply = ctx.sent[0][1], ctx.sent[1][1]
    assert ack.acknowledged_msg_id == msg.msg_id
    assert isinstance(reply.content[0], TextContent)
    assert "queued" in reply.content[0].text
    assert isinstance(reply.content[-1], EndSessionContent)
    assert reply.msg_id == reply_msg_id(str(msg.msg_id)) != msg.msg_id


async def test_redelivered_message_same_reply_and_run() -> None:
    svc, msg = service(), chat(f"verify {PR_URL}")
    first, second = FakeContext(), FakeContext()
    await handle_chat_message(svc, first, SENDER, msg)
    await handle_chat_message(svc, second, SENDER, msg)
    a, b = first.sent[1][1], second.sent[1][1]
    assert a.msg_id == b.msg_id
    assert a.content[0].text == b.content[0].text
    assert second.sent[0][1].acknowledged_msg_id == msg.msg_id


async def test_ack_does_not_reply() -> None:
    ctx = FakeContext()
    await handle_acknowledgement(ctx, SENDER, ChatAcknowledgement(
        timestamp=datetime.now(UTC), acknowledged_msg_id=uuid.uuid4()))
    assert ctx.sent == []


async def test_non_text_parts_ignored_and_malformed_answered() -> None:
    msg = chat("help", extra=[StartSessionContent()])
    assert message_text(msg) == "help"
    ctx = FakeContext()
    empty = ChatMessage(timestamp=datetime.now(UTC), msg_id=uuid.uuid4(),
                        content=[StartSessionContent()])
    await handle_chat_message(service(), ctx, SENDER, empty)
    assert isinstance(ctx.sent[0][1], ChatAcknowledgement)
    assert ctx.sent[1][1].content[0].text
    flood = ChatMessage(timestamp=datetime.now(UTC), msg_id=uuid.uuid4(),
                        content=[TextContent(text="help")] * (MAX_CONTENT_PARTS + 1))
    ctx = FakeContext()
    await handle_chat_message(service(), ctx, SENDER, flood)
    assert "verdict" not in ctx.sent[1][1].content[0].text.lower().split("\n")[0]


def test_protocol_uses_official_chat_spec() -> None:
    protocol = build_chat_protocol(service())
    from uagents import Protocol

    assert protocol.digest == Protocol(spec=chat_protocol_spec).digest
    models = {m.__name__ for m in protocol.models.values()}
    assert {"ChatMessage", "ChatAcknowledgement"} <= models


def test_reply_id_deterministic_uuid4() -> None:
    a = reply_msg_id("abc")
    assert a == reply_msg_id("abc")
    assert a.version == 4
    assert a != reply_msg_id("abd")


def test_offline_main_refuses_to_start_agent(monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PROOFHUNTER_PROFILE", "offline")
    assert main([]) == 2
    assert "not started" in capsys.readouterr().err
