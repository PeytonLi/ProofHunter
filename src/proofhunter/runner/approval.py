"""Local isolation approval checks without provider access."""

import json
from pathlib import Path
from typing import Any

from proofhunter.contracts import canonical_sha256, sha256_hex

SANDBOX_FILES = Path(__file__).resolve().parents[3] / "ops" / "sandbox"
HARNESS_FILES = ("wrapper.py", "canary.py")
HARNESS_DIR = "/opt/proofhunter"
PYTHON = "/usr/local/bin/python3"
OBSERVE_COMMAND = f"{PYTHON} -I {HARNESS_DIR}/wrapper.py"
CANARY_COMMAND = f"{PYTHON} -I {HARNESS_DIR}/canary.py"
# PRD amendment A1: nothing is injected into a sandbox, not even PATH.
ENV: dict[str, str] = {}
PINNED_IMAGE_REPOSITORY = "python:3.12-slim"
ALLOWED_REPOSITORIES = frozenset({"PeytonLi/ProofHunter-Demo"})
AMENDMENT = "PRD-A1-basic-daytona@2026-11-07"
EGRESS_POLICY = "restricted"
CHILD_TARGETS = (
    "172.20.0.1:22",
    "140.82.112.3:443",
    "151.101.0.223:443",
    "1.1.1.1:443",
    "169.254.169.254:80",
    "169.254.170.2:80",
)
METADATA_TARGETS = ("169.254.169.254:80", "169.254.170.2:80")
DNS_NAMES = (
    "github.com",
    "pypi.org",
    "app.daytona.io",
    "api.render.com",
    "metadata.google.internal",
)
CANARY_EXPECTED: dict[str, Any] = {
    "egress_policy": EGRESS_POLICY,
    "dns_denied": True,
    "metadata_denied": True,
    "secrets_absent": True,
    "restricted_child": True,
    "child_network_isolated": True,
    "probes_denied": {
        **{f"dns:{name}": True for name in DNS_NAMES},
        **{f"metadata:{target}": True for target in METADATA_TARGETS},
        **{f"child:{target}": True for target in CHILD_TARGETS},
    },
}


def pinned_image(image_digest: str) -> str:
    return f"{PINNED_IMAGE_REPOSITORY}@{image_digest}"


def allowed_repository(full_name: str) -> bool:
    return full_name in ALLOWED_REPOSITORIES


def valid_canary(data: Any) -> bool:
    if type(data) is not dict or set(data) != set(CANARY_EXPECTED):
        return False
    probes = data["probes_denied"]
    return (
        data["egress_policy"] == EGRESS_POLICY
        and all(
            data[key] is True
            for key in CANARY_EXPECTED
            if key not in ("probes_denied", "egress_policy")
        )
        and type(probes) is dict
        and set(probes) == set(CANARY_EXPECTED["probes_denied"])
        and all(value is True for value in probes.values())
    )


def harness_files() -> dict[str, bytes]:
    return {name: (SANDBOX_FILES / name).read_bytes() for name in HARNESS_FILES}


def harness_sha256(files: dict[str, bytes] | None = None) -> str:
    files = harness_files() if files is None else files
    return sha256_hex(b"".join(files[name] for name in HARNESS_FILES))


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
            "secrets": {},
            "ephemeral": True,
            "egress_policy": EGRESS_POLICY,
            "amendment": AMENDMENT,
            "allowed_repositories": sorted(ALLOWED_REPOSITORIES),
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
        if tier < 3 and approval.get("amendment") != AMENDMENT:
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
                "amendment": AMENDMENT,
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
