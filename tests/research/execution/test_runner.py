from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from bench.corpus import ROOT, load
from proofhunter.contracts import ObservationOutcome, Side
from research.execution import commands
from research.execution.allowlist import ReviewedSourceAllowlist
from research.execution.pool import shared_pool
from research.execution.runner import TrackARunner, make_runner
from research.execution.sandbox import LocalTemplateSandbox
from research.schemas.interfaces import EpisodeRunner
from research.schemas.models import MeasurementMode, RawResultRow

BENCH = [c.case_id for c in load(ROOT).cases]
SEED = 1633385816


@dataclass
class Picker:
    arm_id: str
    seed: int = 0

    def reset(self, context):
        self.rng = random.Random(self.seed)
        self.context = context

    def choose(self, state):
        assert len(state.history) == state.step - 1
        pool = sorted(state.available, key=lambda c: c.input_sha256)
        if self.arm_id == "first":
            return pool[0].input_sha256
        if self.arm_id == "last":
            return pool[-1].input_sha256
        return self.rng.choice(pool).input_sha256


class FlakySandbox(LocalTemplateSandbox):
    """Allowlist gate intact; process replaced by a scripted infra outcome."""

    script: tuple[ObservationOutcome, ...] = ()

    def _run(self, request, timing):
        n = self.invocations - 1
        outcome = self.script[n % len(self.script)] if self.script else ObservationOutcome.TIMEOUT
        timing.cleaned = True
        if outcome is ObservationOutcome.OK:
            return outcome, 0, b'{"value":0}', b"", False, 0
        return outcome, None, b"", b"", False, None


def _runner(protocol, allowlist, sandbox=LocalTemplateSandbox, script=()):
    def factory(a, e):
        box = sandbox(a, e, network_namespace=False)
        box.script = script
        return box
    return TrackARunner(protocol, ledger=commands.JsonlLedger(None),  # type: ignore[arg-type]
                        allowlist=allowlist, sandbox_factory=factory)


def _check_budget(row: RawResultRow, runner: TrackARunner) -> None:
    inv = row.invocations
    assert len(row.probes) <= 6
    assert inv.confirm <= 1
    assert inv.base + inv.head + inv.confirm <= 15
    assert sum(p.retries for p in row.probes) <= 2
    assert len({p.candidate_input_sha256 for p in row.probes}) == len(row.probes)
    trace = runner.last_trace
    assert trace is not None
    assert len(trace.observations) == inv.base + inv.head + inv.confirm
    assert inv.failed == sum(not o.usable for o in trace.observations)
    RawResultRow.model_validate_json(row.model_dump_json())


def test_make_runner_implements_frozen_interface(protocol):
    assert isinstance(make_runner(protocol, commands.JsonlLedger(None)), EpisodeRunner)


@pytest.mark.parametrize("arm", ["first", "last", "random"])
@pytest.mark.parametrize("script", [
    (),  # every attempt times out
    (ObservationOutcome.TIMEOUT, ObservationOutcome.OK),
    (ObservationOutcome.OK, ObservationOutcome.INFRA_ERROR, ObservationOutcome.NONZERO),
    (ObservationOutcome.OK,),
])
def test_no_arm_exceeds_budget_under_failures(protocol, allowlist, case_sources, arm, script):
    runner = _runner(protocol, allowlist, FlakySandbox, script)
    for case_id in BENCH:
        row = runner.run(case_sources(case_id), Picker(arm, 7), None, arm_id=arm, seed=SEED,
                         repetition=0, mode=MeasurementMode.OFFLINE)
        _check_budget(row, runner)
        if not script:
            assert row.invocations.failed == row.invocations.base + row.invocations.head
            assert sum(p.retries for p in row.probes) == 2
            assert row.verdict == "inconclusive"


@pytest.mark.parametrize("arm", ["first", "last", "random"])
def test_real_execution_rows_valid_and_timed(protocol, allowlist, case_sources, arm):
    runner = _runner(protocol, allowlist)
    for case_id in BENCH:
        row = runner.run(case_sources(case_id), Picker(arm, 3), None, arm_id=arm, seed=SEED,
                         repetition=1, mode=MeasurementMode.OFFLINE)
        _check_budget(row, runner)
        t = row.timings
        assert t.sandbox_startup_ms
        assert t.execution_ms
        assert t.cleanup_ms is not None
        assert (t.confirmation_ms is not None) == (row.invocations.confirm == 1)
        assert row.cost_usd == 0.0
        assert row.pool_sha256 is not None
        if case_id.startswith("clean"):
            assert row.verdict not in ("confirmed_regression", "confirmed_existing_defect")


def test_fixed_order_reproduces_bench_regression(protocol, allowlist, case_sources):
    row = _runner(protocol, allowlist).run(
        case_sources("per_line"), commands.FixedOrderSelector(), None, arm_id="fixed_order",
        seed=SEED, repetition=0, mode=MeasurementMode.OFFLINE)
    assert row.verdict == "confirmed_regression"
    assert row.invocations.confirm == 1
    assert row.pairs_to_first_confirmed_witness == len(row.probes)
    assert not row.censored


def test_non_allowlisted_case_is_refused_without_running(protocol, case_sources, monkeypatch):
    from research.execution import sandbox as sb

    runner = _runner(protocol, ReviewedSourceAllowlist({}, "empty"))
    monkeypatch.setattr(sb.subprocess, "Popen", lambda *a, **k: pytest.fail("process started"))
    row = runner.run(case_sources("per_line"), Picker("first"), None, arm_id="first", seed=SEED,
                     repetition=0, mode=MeasurementMode.OFFLINE)
    assert row.verdict == "inconclusive"
    assert row.reason_code == "isolation_unverified"
    assert len(row.probes) == 1
    assert row.invocations.failed == 2
    assert row.invocations.base == row.invocations.head == 1  # denials counted, never retried


def test_default_runner_refuses_without_reviewed_template_registry(protocol, case_sources):
    from research.execution.allowlist import ExecutionRefused

    with pytest.raises(ExecutionRefused):
        make_runner(protocol, commands.JsonlLedger(None)).run(
            case_sources("per_line"), Picker("first"), None, arm_id="first", seed=SEED,
            repetition=0, mode=MeasurementMode.OFFLINE)


def test_selector_cannot_escape_pool(protocol, allowlist, case_sources):
    class Rogue(Picker):
        def choose(self, state):
            return "f" * 64
    with pytest.raises(ValueError, match="outside the guarded pool"):
        _runner(protocol, allowlist).run(case_sources("per_line"), Rogue("rogue"), None,
                                         arm_id="rogue", seed=SEED, repetition=0,
                                         mode=MeasurementMode.OFFLINE)


def test_pool_identical_across_arms_and_seed_dependent(protocol, allowlist, case_sources):
    case = case_sources("per_line").case
    assert shared_pool(case, protocol, SEED) == shared_pool(case, protocol, SEED)
    other = shared_pool(case, protocol, 1)
    assert shared_pool(case, protocol, SEED).bank_sha256 != other.bank_sha256
    rows = [_runner(protocol, allowlist).run(case_sources("per_line"), Picker(a), None, arm_id=a,
                                             seed=SEED, repetition=0,
                                             mode=MeasurementMode.OFFLINE)
            for a in ("first", "last")]
    assert rows[0].pool_sha256 == rows[1].pool_sha256


def test_confirmation_is_fresh_head_replay(protocol, allowlist, case_sources):
    runner = _runner(protocol, allowlist)
    runner.run(case_sources("per_line"), commands.FixedOrderSelector(), None,
               arm_id="fixed_order", seed=SEED, repetition=0, mode=MeasurementMode.OFFLINE)
    obs = runner.last_trace.observations
    confirm = [o for o in obs if o.side is Side.CONFIRM]
    head = [o for o in obs if o.side is Side.HEAD]
    assert len(confirm) == 1
    assert confirm[0].attempt_id != head[-1].attempt_id
    assert confirm[0].source_sha256 == head[-1].source_sha256
    assert confirm[0].input_sha256 == head[-1].input_sha256


def test_run_pilot_bench_plumbing(tmp_path):
    assert commands.run_pilot(["--bench-plumbing", "--out", str(tmp_path)]) == 0
    rows = [RawResultRow.model_validate_json(x)
            for x in (tmp_path / "raw_results.jsonl").read_text().splitlines()]
    assert len(rows) == len(BENCH)
    entries = commands.JsonlLedger(tmp_path / "ledger.jsonl").entries()
    assert len(entries) == 1
    assert entries[0].paid is False
    assert entries[0].cost_usd == 0.0


def test_run_pilot_refuses_test_split(tmp_path):
    with pytest.raises(SystemExit):
        commands.run_pilot(["--store", str(tmp_path), "--splits", "test"])
