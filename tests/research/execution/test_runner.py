"""Integration lane: episode runner, shared pool, R4 fault drills and exact budgets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proofhunter.contracts import ObservationOutcome
from research.execution import doubles
from research.execution.pool import shared_pool
from research.execution.sandbox import ReviewedTemplateExecutor
from research.schemas.models import (
    CostStatus,
    LedgerCategory,
    LedgerEntry,
    MeasurementMode,
    ProtocolV1,
    RawResultRow,
)

REPO = Path(__file__).resolve().parents[3]
OFFLINE = MeasurementMode.OFFLINE


@pytest.fixture(scope="module")
def protocol() -> ProtocolV1:
    return ProtocolV1.model_validate_json(
        (REPO / "research/spec/protocol-v1.json").read_text())


def run(protocol: ProtocolV1, case: str, selector: object, *, faults: dict | None = None,
        generator: object = None, arm_id: str | None = None,
        mode: MeasurementMode = OFFLINE, runner: object = None) -> RawResultRow:
    runner = runner or doubles.test_runner(protocol, faults=faults)
    row = runner.run(doubles.bench_sources(case), selector, generator,  # type: ignore[attr-defined]
                     arm_id=arm_id or selector.arm_id, seed=1, repetition=0,  # type: ignore[attr-defined]
                     mode=mode)
    assert RawResultRow.model_validate_json(row.model_dump_json()) == row
    return row


def total(row: RawResultRow) -> int:
    return row.invocations.base + row.invocations.head + row.invocations.confirm


def test_pool_is_shared_and_frozen(protocol: ProtocolV1) -> None:
    sources = doubles.bench_sources("per_line")
    bank = shared_pool(sources.case, protocol)
    assert bank == shared_pool(sources.case, protocol)
    assert len(bank.candidates) <= protocol.candidate_pool["max_candidates"]
    rows = [run(protocol, "per_line", s) for s in (doubles.FixedOrderDouble(),
                                                   doubles.EigDouble())]
    assert {r.pool_sha256 for r in rows} == {bank.bank_sha256}


@pytest.mark.parametrize("case,verdict", [
    ("per_line", "confirmed_regression"), ("half_even", "confirmed_regression"),
    ("existing", "confirmed_existing_defect"), ("clean_divmod", "no_defect_found"),
])
def test_verdicts_follow_production_rules(protocol: ProtocolV1, case: str, verdict: str) -> None:
    row = run(protocol, case, doubles.EigDouble())
    assert row.verdict.value == verdict  # type: ignore[union-attr]
    assert row.cost_status is CostStatus.MEASURED and row.cost_usd == 0.0
    assert row.snap_routing == "disabled"


def test_exact_budget_with_every_retry_spent(protocol: ProtocolV1) -> None:
    faults = {("base", n): ObservationOutcome.TIMEOUT for n in range(1, 20)}
    row = run(protocol, "clean_divmod", doubles.FixedOrderDouble(), faults=faults)
    assert len(row.probes) == protocol.max_pairs
    assert sum(p.retries for p in row.probes) == protocol.max_retries_per_episode
    assert total(row) == 2 * protocol.max_pairs + protocol.max_retries_per_episode
    assert total(row) <= protocol.max_invocations_per_episode
    assert row.invocations.failed == protocol.max_pairs + protocol.max_retries_per_episode
    assert all(p.outcome.value == "unusable" for p in row.probes)
    assert row.verdict.value == "inconclusive"  # type: ignore[union-attr]


def test_transient_timeout_is_retried_once(protocol: ProtocolV1) -> None:
    row = run(protocol, "per_line", doubles.EigDouble(),
              faults={("head", 1): ObservationOutcome.TIMEOUT})
    assert row.probes[0].retries == 1
    assert row.verdict.value == "confirmed_regression"  # type: ignore[union-attr]
    assert row.invocations.failed == 1 and total(row) == 4


def test_nonretryable_invalid_output_is_unusable(protocol: ProtocolV1) -> None:
    row = run(protocol, "per_line", doubles.EigDouble(),
              faults={("head", 1): ObservationOutcome.INVALID})
    assert row.probes[0].outcome.value == "unusable" and row.probes[0].retries == 0


def test_flaky_confirmation_is_not_a_witness(protocol: ProtocolV1) -> None:
    row = run(protocol, "per_line", doubles.EigDouble(), faults={("confirm", 1): 999999})
    assert row.probes[0].confirmation.value == "not_reproduced"
    assert row.censored and row.invocations.confirm == 1
    assert row.verdict.value == "inconclusive"  # type: ignore[union-attr]


def test_confirmation_timeout_is_unusable(protocol: ProtocolV1) -> None:
    row = run(protocol, "per_line", doubles.EigDouble(),
              faults={("confirm", 1): ObservationOutcome.TIMEOUT})
    assert row.probes[0].confirmation.value == "unusable" and row.censored


def test_real_deadline_times_out_reviewed_code() -> None:
    from proofhunter.contracts import CandidateInput

    source = b"def total_cents(prices_cents, discount_percent):\n    while True:\n        pass\n"
    import hashlib

    executor = ReviewedTemplateExecutor({hashlib.sha256(source).hexdigest()}.__contains__,
                                        timeout_seconds=0.2)
    result = executor("head", source, CandidateInput(prices_cents=[1], discount_percent=0))
    assert result.outcome is ObservationOutcome.TIMEOUT and result.value is None
    assert result.duration_ms < 2000


def test_unreviewed_source_never_executes(protocol: ProtocolV1) -> None:
    from proofhunter.contracts import CandidateInput

    executor = ReviewedTemplateExecutor(lambda digest: False, timeout_seconds=1)
    result = executor("base", b"raise SystemExit(1)\n",
                      CandidateInput(prices_cents=[], discount_percent=0))
    assert result.outcome is ObservationOutcome.DENIED
    runner = doubles.test_runner(protocol)
    runner.review = lambda sources: "template_not_reviewed"
    row = run(protocol, "per_line", doubles.FixedOrderDouble(), runner=runner)
    assert (row.verdict.value, row.reason_code, total(row)) == (  # type: ignore[union-attr]
        "inconclusive", "source_invalid", 0)


def test_selector_outage_and_malformed_choice(protocol: ProtocolV1) -> None:
    outage = run(protocol, "clean_divmod",
                 doubles.ScriptedSelector("fixed_order", fail_reset=True))
    assert (outage.reason_code, total(outage)) == ("provider_unavailable", 0)
    midway = run(protocol, "clean_divmod", doubles.ScriptedSelector("fixed_order", fail_at=3))
    assert len(midway.probes) == 2 and midway.verdict.value == "inconclusive"  # type: ignore[union-attr]
    invalid = run(protocol, "clean_divmod",
                  doubles.ScriptedSelector("fixed_order", invalid_at=1))
    assert (len(invalid.probes), total(invalid)) == (0, 0)
    assert invalid.verdict.value == "inconclusive"  # type: ignore[union-attr]


def test_generator_outage_and_malformed_output_fall_back(protocol: ProtocolV1) -> None:
    for generator, reason in (
        (doubles.StaticGenerator(error=ConnectionError("down")), "generator_unavailable"),
        (doubles.StaticGenerator(error=TimeoutError("slow")), "generator_unavailable"),
        (doubles.StaticGenerator(raw={"hypotheses": "nope"}), "generator_invalid_output"),
    ):
        runner = doubles.test_runner(protocol)
        row = run(protocol, "per_line", doubles.EigDouble(), generator=generator,
                  arm_id="qwen_untuned_eig", runner=runner)
        assert row.verdict.value == "confirmed_regression"  # type: ignore[union-attr]
        assert row.models.generator_model_id == "double-static"
        assert [e["reason"] for e in runner.events] == [reason]
        assert row.timings.generation_ms is not None


def test_reviewer_arm_gets_no_execution_feedback(protocol: ProtocolV1) -> None:
    reviewer = doubles.ScriptedSelector("frontier_reviewer")
    run(protocol, "clean_divmod", reviewer, arm_id="frontier_reviewer")
    assert reviewer.seen_history == [0] * 6
    agent = doubles.ScriptedSelector("frontier_agent")
    run(protocol, "clean_divmod", agent, arm_id="frontier_agent")
    assert agent.seen_history == list(range(6))


def test_model_arm_costs_come_from_the_ledger(protocol: ProtocolV1) -> None:
    unknown = run(protocol, "per_line", doubles.EigDouble(), generator=doubles.StaticGenerator(),
                  arm_id="qwen_untuned_eig", mode=MeasurementMode.LIVE)
    assert (unknown.cost_usd, unknown.cost_status) == (None, CostStatus.UNKNOWN)
    ledger = doubles.MemoryLedger()
    runner = doubles.test_runner(protocol, ledger)
    entry = LedgerEntry.model_validate_json(
        (REPO / "research/schemas/samples/LedgerEntry.json").read_text()).model_copy(update={
            "lane": "baselines", "cost_usd": 0.0125, "cost_status": CostStatus.MEASURED,
            "resource_id": None, "teardown_verified": None,
            "category": LedgerCategory.MODEL_INFERENCE})

    class PayingGenerator(doubles.StaticGenerator):
        def propose(self, prompt):  # type: ignore[no-untyped-def]
            ledger.append(entry)
            return super().propose(prompt)

    paid = run(protocol, "per_line", doubles.EigDouble(), generator=PayingGenerator(),
               arm_id="qwen_untuned_eig", mode=MeasurementMode.LIVE, runner=runner)
    assert (paid.cost_usd, paid.cost_status) == (0.0125, CostStatus.MEASURED)


def test_unknown_arm_is_rejected(protocol: ProtocolV1) -> None:
    with pytest.raises(ValueError, match="not in the frozen protocol"):
        run(protocol, "per_line", doubles.EigDouble(), arm_id="made_up")


def test_rows_never_carry_labels(protocol: ProtocolV1) -> None:
    row = json.loads(run(protocol, "per_line", doubles.EigDouble()).model_dump_json())
    keys = set(row) | set(row["models"]) | {k for p in row["probes"] for k in p}
    assert not {k for k in keys if k.startswith(("label", "witness", "template", "family",
                                                  "expected", "defect", "params"))}


def test_sixteenth_invocation_is_refused(protocol: ProtocolV1) -> None:
    from research.execution.runner import BudgetViolation, Side, _Episode

    calls: list[str] = []
    episode = _Episode("run", None, doubles.bench_sources("per_line"),  # type: ignore[arg-type]
                       lambda *a: calls.append("x"),  # type: ignore[arg-type,return-value]
                       protocol.max_invocations_per_episode, protocol.max_retries_per_episode)
    episode.counts["base"] = protocol.max_invocations_per_episode
    with pytest.raises(BudgetViolation):
        episode.observe(Side.BASE, None)  # type: ignore[arg-type]
    assert calls == []
