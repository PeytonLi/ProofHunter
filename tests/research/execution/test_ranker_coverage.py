"""The ranker lane's history-tag missing coverage agrees with the runner's oracle coverage.

The ranker derives ``missing_coverage`` from match/mismatch history tags; the runner passes
``oracle.compute_coverage`` (exact base = head = expected pairs only). They can only differ after
a mismatch, and the runner never asks for another pair after a mismatch (it confirms and stops),
so every decision state must agree.
"""

from __future__ import annotations

import hashlib
import os
import random
from pathlib import Path

import pytest

import proofhunter.contracts as c
from research.execution import doubles
from research.schemas import models as M
from research.schemas.interfaces import EpisodeContext, EpisodeState
from research.schemas.models import MeasurementMode, ProtocolV1

REPO = Path(__file__).resolve().parents[3]
PROTOCOL = ProtocolV1.model_validate_json((REPO / "research/spec/protocol-v1.json").read_text())
DATA = Path(os.environ.get("PROOFHUNTER_RESEARCH_DATA", Path.home() / "research-data")) / "track_a"


def ranker_missing(context: EpisodeContext, state: EpisodeState) -> list[str]:
    """Verbatim rule from ``research.ranker.collect`` / ``research.ranker.relabel``."""
    pool = context.bank.candidates
    seen = {t for h in state.history if h.outcome in (M.Outcome.MATCH, M.Outcome.MISMATCH)
            for x in pool if x.input_sha256 == h.candidate_input_sha256 for t in x.coverage_tags}
    return [t for t in c.REQUIRED_COVERAGE_TAGS if t not in seen]


class Checking:
    def __init__(self, seed: int) -> None:
        self.seed, self.states = seed, 0

    def reset(self, context: EpisodeContext) -> None:
        self.context = context
        self.rng = random.Random(f"{self.seed}:{context.case_id}")

    def choose(self, state: EpisodeState) -> str:
        assert all(h.outcome is not M.Outcome.MISMATCH for h in state.history)
        assert sorted(ranker_missing(self.context, state)) == sorted(state.missing_coverage)
        self.states += 1
        return self.rng.choice(sorted(x.input_sha256 for x in state.available))


def _check(store, runner, case_ids) -> int:  # type: ignore[no-untyped-def]
    states = 0
    for case_id in case_ids:
        for seed in PROTOCOL.replicate_seeds[:2]:
            selector = Checking(seed)
            runner.run(store.sources(case_id), selector, None, arm_id="uniform_random",
                       seed=seed, repetition=0, mode=MeasurementMode.OFFLINE)
            states += selector.states
    return states


def test_ranker_coverage_matches_oracle_on_bench_pools() -> None:
    store = doubles.BenchStore()
    runner = doubles.offline_runner(PROTOCOL)
    assert _check(store, runner, [x.case_id for x in store.manifest().cases]) > 0


@pytest.mark.skipif(not (DATA / "case-manifest.json").is_file(), reason="dataset not extracted")
def test_ranker_coverage_matches_oracle_on_validation_pools() -> None:
    from research.data.store import open_store
    from research.execution.runner import make_runner

    store = open_store(DATA)
    cases = sorted((x.case_id for x in store.manifest().cases if x.split is M.Split.VALIDATION),
                   key=lambda k: hashlib.sha256(k.encode()).hexdigest())[:40]
    assert _check(store, make_runner(PROTOCOL, doubles.MemoryLedger()), cases) > 0


def test_addon_arm_reuses_base_spec_and_records_addon_id() -> None:
    runner = doubles.offline_runner(PROTOCOL)
    runner.addons = {"learned_ranker_no_belief": "builtin_ranker"}
    spec = runner.arm("learned_ranker_no_belief")
    base = runner.arm("builtin_ranker")
    assert spec.arm_id == "learned_ranker_no_belief"
    assert spec.model_dump(exclude={"arm_id"}) == base.model_dump(exclude={"arm_id"})
    assert "learned_ranker_no_belief" in runner.arm_specs()
    with pytest.raises(ValueError, match="not in the frozen protocol"):
        runner.arm("not_an_arm")
