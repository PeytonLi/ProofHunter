"""Local isolation approval checks without provider access."""

import json
from pathlib import Path
from typing import Any

from proofhunter.contracts import canonical_sha256, sha256_hex

SANDBOX_FILES = Path(__file__).resolve().parents[3] / "ops" / "sandbox"
OBSERVE_COMMAND = "/usr/bin/sudo -n /usr/bin/python3 -I /opt/proofhunter/wrapper.py"
CANARY_COMMAND = "/usr/bin/sudo -n /usr/bin/python3 -I /opt/proofhunter/canary.py"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}
CANARY_EXPECTED: dict[str, Any] = {
    "egress_denied": True,
    "metadata_denied": True,
    "secrets_absent": True,
    "restricted_child": True,
    "probes_denied": dict.fromkeys(
        (
            "github.com",
            "raw.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "registry.npmjs.org",
            "1.1.1.1",
            "app.daytona.io",
            "169.254.169.254",
            "169.254.170.2",
        ),
        True,
    ),
}


def valid_canary(data: Any) -> bool:
    if type(data) is not dict or set(data) != set(CANARY_EXPECTED):
        return False
    probes = data["probes_denied"]
    return (
        all(data[key] is True for key in CANARY_EXPECTED if key != "probes_denied")
        and type(probes) is dict
        and set(probes) == set(CANARY_EXPECTED["probes_denied"])
        and all(value is True for value in probes.values())
    )


def harness_sha256() -> str:
    return sha256_hex(
        b"".join((SANDBOX_FILES / name).read_bytes() for name in ("wrapper.py", "canary.py"))
    )


def strict_json(raw: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs)


def config_fingerprint(snapshot: str, image_digest: str, harness: str, deployment: str) -> str:
    return canonical_sha256(
        {
            "snapshot": snapshot,
            "image_digest": image_digest,
            "harness": harness,
            "deployment": deployment,
            "network_block_all": True,
            "public": False,
            "env": ENV,
            "cpu": 1,
            "memory_gib": 1,
            "uid": 65534,
        }
    )


def validate_approval(raw: str, *, fingerprint: str, image_digest: str) -> str | None:
    """Validate bounded local evidence; return only sanitized refusal codes."""
    try:
        if len(raw.encode()) > 4096:
            return "approval_invalid"
        approval = strict_json(raw)
        if type(approval) is not dict:
            return "approval_invalid"
        tier = approval.get("daytona_account_tier")
        if type(tier) is not int or tier < 1:
            return "account_tier_invalid"
        if tier < 3:
            return "account_tier_insufficient"
        if "canary_result" not in approval:
            return "canary_result_missing"
        if not valid_canary(approval["canary_result"]):
            return "canary_result_not_denied"
        if (
            approval
            != {
                "config_fingerprint": fingerprint,
                "image_digest": image_digest,
                "effective_policy_reviewed": True,
                "daytona_account_tier": tier,
                "canary_result": CANARY_EXPECTED,
            }
            or approval["effective_policy_reviewed"] is not True
        ):
            return "approval_invalid"
        return None
    except ValueError:
        return "approval_invalid"


def local_approval_refusal(state_dir: Path, *, fingerprint: str, image_digest: str) -> str | None:
    try:
        with (state_dir / "runner-isolation-approval.json").open(encoding="utf-8") as stream:
            raw = stream.read(4097)
        return validate_approval(raw, fingerprint=fingerprint, image_digest=image_digest)
    except (OSError, ValueError):
        return "approval_invalid"
