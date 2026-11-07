"""Frozen protocol, spend policy, production isolation and CLI skeleton (setup lane)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from proofhunter import bayes, contracts
from research import cli
from research.schemas import interfaces
from research.schemas.models import (
    MAX_INVOCATIONS_PER_EPISODE,
    ProtocolV1,
    SpendPolicy,
)

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "research" / "spec"


def protocol() -> ProtocolV1:
    return ProtocolV1.model_validate_json((SPEC / "protocol-v1.json").read_text())


def test_protocol_matches_sample_and_production_budget() -> None:
    p = protocol()
    sample = (REPO / "research/schemas/samples/ProtocolV1.json").read_text()
    assert json.loads(sample) == json.loads((SPEC / "protocol-v1.json").read_text())
    limits = contracts.BudgetLimits()
    assert (p.max_pairs, p.max_confirmations) == (limits.max_pairs, limits.max_confirmations)
    assert p.max_invocations_per_episode == MAX_INVOCATIONS_PER_EPISODE
    assert p.max_invocations_per_episode <= limits.max_sandbox_invocations
    assert p.invocation_timeout_seconds == limits.invocation_timeout_seconds
    assert {t.value for t in p.required_clean_coverage} == {
        "empty",
        "zero_discount",
        "full_discount",
        "half_cent",
        "multi_item",
    }
    assert p.candidate_pool["generator_version"] == contracts.GENERATOR_VERSION


def test_protocol_arms_cover_required_set() -> None:
    arms = {a.arm_id: a for a in protocol().arms}
    assert {
        "fixed_order",
        "uniform_random",
        "builtin_eig",
        "builtin_ranker",
        "simple_heuristic",
        "qwen_untuned_eig",
        "qwen_untuned_ranker",
        "qwen_tuned_eig",
        "qwen_tuned_ranker",
        "meta_muse_spark_eig",
        "frontier_reviewer",
        "frontier_agent",
    } <= set(arms)
    assert all(
        a.candidate_freedom == "shared_pool"
        for a in arms.values()
        if a.arm_id != "frontier_agent_free_input"
    )
    assert not arms["frontier_reviewer"].executes
    for arm in arms.values():
        if arm.kind.value == "frontier":
            assert arm.model_status == "frozen_by_baselines_lane"


def test_seeds_are_preregistered() -> None:
    import random

    p = protocol()
    rng = random.Random(p.master_seed)
    assert p.replicate_seeds == [rng.randrange(2**32) for _ in range(5)]


def test_shared_pool_is_deterministic_and_plan_independent() -> None:
    plan = contracts.HypothesisPlan(
        hypotheses=contracts.builtin_hypothesis_payload().hypotheses,
        provenance=contracts.ProviderProvenance.model_validate_json(
            (REPO / "schemas/samples/ProviderProvenance.json").read_text()
        ),
    )
    seed = protocol().replicate_seeds[0]
    first, second = bayes.generate(plan, seed), bayes.generate(plan, seed)
    assert first.bank_sha256 == second.bank_sha256
    assert 0 < len(first.candidates) <= protocol().candidate_pool["max_candidates"]


def test_production_files_unchanged() -> None:
    for path, digest in protocol().production_lock_sha256.items():
        assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == digest, path


def test_spend_policy() -> None:
    policy = SpendPolicy.model_validate_json((SPEC / "spend-policy.json").read_text())
    assert policy.status == "user_approved_uncapped"
    assert policy.total_cap_usd is None
    providers = {p.provider for p in policy.providers}
    assert {"openai", "fireworks", "runpod"} <= providers


@pytest.mark.parametrize("command", sorted(cli.COMMANDS))
def test_cli_stubs_exit_nonzero(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    if cli.resolve(command) is not None:
        pytest.skip("implemented by its owning lane")
    assert cli.main([command]) == cli.NOT_IMPLEMENTED_EXIT
    assert "not implemented" in capsys.readouterr().err


def test_cli_usage_errors() -> None:
    assert cli.main([]) == cli.USAGE_EXIT
    assert cli.main(["train-everything"]) == cli.USAGE_EXIT


def test_cli_lists_section_16_commands() -> None:
    assert len(cli.COMMANDS) == 10
    lanes = {spec.lane for spec in cli.COMMANDS.values()}
    assert lanes <= {
        "dataset",
        "ranker",
        "generator",
        "baselines",
        "measurement",
        "integration",
        "verification",
    }
    for spec in cli.COMMANDS.values():
        assert spec.module.startswith("research.")


def test_interfaces_are_protocols() -> None:
    for name in (
        "Selector",
        "HypothesisGenerator",
        "FeatureExtractor",
        "CaseStore",
        "EpisodeRunner",
        "Ledger",
        "RankerTrainer",
        "GeneratorTrainer",
    ):
        assert getattr(getattr(interfaces, name), "_is_protocol", False), name
