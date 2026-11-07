"""One-shot Agentverse account connection using the pinned SDK connect flow."""

from __future__ import annotations

import os
from dataclasses import dataclass

from uagents.crypto import Identity
from uagents.mailbox import AgentverseConnectRequest, register_in_agentverse
from uagents_core.config import AgentverseConfig
from uagents_core.registration import RegistrationRequest

from proofhunter.ports import ConfigError


def registration_key() -> str:
    key = os.environ.get("AGENTVERSE_API_KEY", "").strip()
    if not key:
        raise ConfigError("AGENTVERSE_API_KEY is required for register")
    return key


@dataclass(frozen=True)
class RegistrationResult:
    success: bool
    detail: str


async def register_mailbox(seed: str, key: str) -> RegistrationResult:
    """Connect a private mailbox without constructing or starting a public Agent."""
    identity = Identity.from_seed(seed, 0)
    request = AgentverseConnectRequest(user_token=key, agent_type="mailbox")
    details = RegistrationRequest(
        address=identity.address, name="ProofHunter staging mailbox", agent_type="mailbox",
    )
    try:
        result = await register_in_agentverse(
            request=request, identity=identity, prefix="agent",
            agentverse=AgentverseConfig(), agent_details=details,
        )
    except Exception:
        return RegistrationResult(False, "transport_or_response_error")
    if result.success:
        return RegistrationResult(True, "connected")
    remote = (result.detail or "").lower()
    detail = "remote_rejected"
    if "forbidden" in remote or "permission" in remote:
        detail = "permission_denied"
    elif "unauthorized" in remote or "authentication" in remote:
        detail = "authentication_failed"
    return RegistrationResult(False, detail)
