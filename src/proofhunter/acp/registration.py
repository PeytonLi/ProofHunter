"""One-shot Agentverse account connection using the pinned SDK connect flow."""

from __future__ import annotations

import os
from typing import Any

from uagents.mailbox import AgentverseConnectRequest, register_in_agentverse
from uagents_core.registration import RegistrationRequest

from proofhunter.ports import ConfigError


def registration_key() -> str:
    key = os.environ.get("AGENTVERSE_API_KEY", "").strip()
    if not key:
        raise ConfigError("AGENTVERSE_API_KEY is required for register")
    return key


async def register_mailbox(agent: Any, key: str) -> bool:
    """Mirror uagents 0.26.1's inspector /connect handler; discard remote detail."""
    request = AgentverseConnectRequest(user_token=key, agent_type="mailbox")
    details = RegistrationRequest(
        address=agent.address, name=agent.name, handle=agent._handle,
        url=request.endpoint, profile=agent._build_registration_profile(),
        endpoints=agent._endpoints, protocols=list(agent.protocols.keys()),
        metadata=agent.metadata,
    )
    try:
        result = await register_in_agentverse(
            request=request, identity=agent.identity, prefix=agent._prefix,
            agentverse=agent.agentverse, agent_details=details,
        )
    except Exception:
        return False
    return bool(result.success)
