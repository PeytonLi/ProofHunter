"""Frozen cross-lane interfaces (research-protocol-v1). Lanes implement; only setup edits."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from proofhunter.contracts import Candidate, CandidateBank, CoverageTag, HypothesisPlanPayload
from research.schemas.models import (
    CaseLabel,
    CaseManifest,
    CaseRecord,
    GeneratorExample,
    GeneratorInput,
    LedgerEntry,
    MeasurementMode,
    Outcome,
    RankingObservation,
    RawResultRow,
)

FeatureVector = Mapping[str, float | int | bool]


@dataclass(frozen=True)
class CaseSources:
    """Reviewed template output for one case. Never contains labels or template params."""

    case: CaseRecord
    base_source: bytes
    head_source: bytes


@dataclass(frozen=True)
class PairFeedback:
    """What every executing arm sees after a pair (identical across arms)."""

    step: int
    candidate_input_sha256: str
    outcome: Outcome
    expected_cents: int | None
    base_cents: int | None
    head_cents: int | None


@dataclass(frozen=True)
class EpisodeContext:
    """Pre-execution context, identical for every arm on a case and seed."""

    case_id: str
    seed: int
    base_source: bytes
    head_source: bytes
    unified_diff: str
    bank: CandidateBank
    plan: HypothesisPlanPayload


@dataclass(frozen=True)
class EpisodeState:
    step: int
    remaining_pairs: int
    available: Sequence[Candidate]
    missing_coverage: Sequence[CoverageTag]
    history: Sequence[PairFeedback]


@runtime_checkable
class Selector(Protocol):
    """Chooses the next candidate from ``state.available`` (already coverage-guarded)."""

    arm_id: str

    def reset(self, context: EpisodeContext) -> None: ...

    def choose(self, state: EpisodeState) -> str:
        """Return the ``input_sha256`` of one candidate in ``state.available``."""
        ...


@runtime_checkable
class HypothesisGenerator(Protocol):
    generator_id: str

    def propose(self, prompt: GeneratorInput) -> HypothesisPlanPayload: ...


@runtime_checkable
class FeatureExtractor(Protocol):
    feature_set_version: str

    def extract(
        self, context: EpisodeContext, state: EpisodeState, candidate: Candidate
    ) -> FeatureVector: ...


@runtime_checkable
class CaseStore(Protocol):
    """Dataset lane. ``labels`` refuses the sealed test split unless ``unseal`` is given."""

    def manifest(self) -> CaseManifest: ...

    def sources(self, case_id: str) -> CaseSources: ...

    def labels(self, split: str, *, unseal: str | None = None) -> Sequence[CaseLabel]: ...


@runtime_checkable
class EpisodeRunner(Protocol):
    """Integration lane: one (case, arm, seed, repetition) episode under the frozen budget."""

    def run(
        self,
        sources: CaseSources,
        selector: Selector,
        generator: HypothesisGenerator | None,
        *,
        arm_id: str,
        seed: int,
        repetition: int,
        mode: MeasurementMode,
    ) -> RawResultRow: ...


@runtime_checkable
class Ledger(Protocol):
    def append(self, entry: LedgerEntry) -> None: ...

    def entries(self) -> Sequence[LedgerEntry]: ...


@runtime_checkable
class RankerTrainer(Protocol):
    def train(
        self,
        train: Iterable[RankingObservation],
        validation: Iterable[RankingObservation],
        out_dir: Path,
    ) -> str:
        """Fit, select on validation only, write the checkpoint; return its sha256."""
        ...


@runtime_checkable
class GeneratorTrainer(Protocol):
    def train(
        self,
        train: Iterable[GeneratorExample],
        validation: Iterable[GeneratorExample],
        out_dir: Path,
    ) -> str:
        """Train the adapter, select on validation only; return the adapter sha256."""
        ...
