"""Synthetic local evidence only: never authorizes a real sandbox."""

import json
from pathlib import Path
from typing import Any

import pytest

from proofhunter import cli
from proofhunter import contracts as c
from proofhunter.config import ServiceRole, load_settings
from proofhunter.ports import ConfigError
from proofhunter.runner.approval import (
    AMENDMENT,
    CANARY_EXPECTED,
    config_fingerprint,
    harness_sha256,
)
from proofhunter.runner.daytona import DaytonaRunner
from tests.integration.test_live_cli import environment, trusted


@pytest.mark.parametrize(
    "fault",
    [
        "valid",
        "missing",
        "malformed",
        "empty",
        "duplicate",
        "oversize",
        "image",
        "snapshot",
        "harness",
        "tier",
        "canary_missing",
        "probe_missing",
        *CANARY_EXPECTED["probes_denied"],
    ],
)
def test_startup_local_approval(
    tmp_path: Path, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path)
    env.update(
        {
            "DAYTONA_SNAPSHOT": "synthetic-test-only-snapshot",
            "DAYTONA_IMAGE_DIGEST": "sha256:" + "a" * 64,
        }
    )
    settings = load_settings(env, role=ServiceRole.API)
    settings = settings.model_copy(
        update={"daytona": settings.daytona.model_copy(update={"enable_live_runner": True})}
    )
    assert settings.daytona.api_key is None
    image = settings.daytona.image_digest
    snapshot = settings.daytona.snapshot
    assert image is not None
    assert snapshot is not None
    harness = harness_sha256()
    deployment = c.canonical_sha256(str(settings.db_path.resolve()))[:24]
    fingerprint = config_fingerprint(snapshot, image, harness, deployment)
    approval: dict[str, Any] = {
        "config_fingerprint": fingerprint,
        "image_digest": image,
        "effective_policy_reviewed": True,
        "daytona_account_tier": 1,
        "amendment": AMENDMENT,
        "canary_result": json.loads(json.dumps(CANARY_EXPECTED)),
    }
    if fault == "image":
        approval["image_digest"] = "sha256:" + "b" * 64
    if fault in ("snapshot", "harness"):
        approval["config_fingerprint"] = config_fingerprint(
            "other-snapshot" if fault == "snapshot" else snapshot,
            image,
            "0" * 64 if fault == "harness" else harness,
            deployment,
        )
    if fault == "tier":
        del approval["amendment"]
    if fault == "canary_missing":
        del approval["canary_result"]
    if fault == "probe_missing":
        del approval["canary_result"]["probes_denied"]["metadata:169.254.169.254:80"]
    if fault in CANARY_EXPECTED["probes_denied"]:
        approval["canary_result"]["probes_denied"][fault] = False
    raw = json.dumps(approval)
    raw = {
        "malformed": "{",
        "empty": "",
        "oversize": "x" * 4097,
        "duplicate": raw[:-1] + ',"image_digest":"duplicate"}',
    }.get(fault, raw)
    if fault != "missing":
        (tmp_path / "runner-isolation-approval.json").write_text(raw)
    enrollment = trusted().model_copy(
        update={
            "snapshot_id": snapshot,
            "image_digest": image,
            "repo_full_name": "PeytonLi/ProofHunter-Demo",
        }
    )
    monkeypatch.setattr(
        "proofhunter.runner.daytona.Daytona",
        lambda *a, **kw: pytest.fail("startup constructed SDK"),
    )
    runner = DaytonaRunner(
        None, snapshot=snapshot, image_digest=image, deployment=deployment, state_dir=tmp_path
    )
    if fault == "valid":
        cli.live_trust_gate(settings, [enrollment])
        assert runner._approved()
    else:
        with pytest.raises(
            ConfigError,
            match=r"^(approval_invalid|account_tier_insufficient|canary_result_missing|"
            r"canary_result_not_denied)$",
        ):
            cli.live_trust_gate(settings, [enrollment])
        assert not runner._approved()


def test_disabled_startup_without_approval(tmp_path: Path) -> None:
    settings = load_settings(environment(tmp_path), role=ServiceRole.API)
    cli.live_trust_gate(settings, [trusted()])


def test_offline_startup_without_approval(tmp_path: Path) -> None:
    settings = load_settings({"PROOFHUNTER_PROFILE": "offline"})
    cli.live_trust_gate(settings, [trusted()])


def test_live_runner_allowlist_is_demo_repository_only(tmp_path: Path) -> None:
    settings = load_settings(
        {
            **environment(tmp_path),
            "DAYTONA_SNAPSHOT": "synthetic-test-only-snapshot",
            "DAYTONA_IMAGE_DIGEST": "sha256:" + "a" * 64,
        },
        role=ServiceRole.API,
    )
    settings = settings.model_copy(
        update={"daytona": settings.daytona.model_copy(update={"enable_live_runner": True})}
    )
    for name in ("team/checkout", "peytonli/proofhunter-demo", "PeytonLi/ProofHunter"):
        enrollment = trusted().model_copy(
            update={
                "snapshot_id": "synthetic-test-only-snapshot",
                "image_digest": "sha256:" + "a" * 64,
                "repo_full_name": name,
            }
        )
        with pytest.raises(ConfigError, match=r"^repository not on basic-tier allowlist$"):
            cli.live_trust_gate(settings, [enrollment])
