"""Research schemas v1. Changes require a new schema version owned by the setup lane.

Production DTOs from ``proofhunter.contracts`` are reused read-only; nothing here
widens a production enum, likelihood policy or hypothesis taxonomy.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

from proofhunter.contracts import (
    CONTRACT_ID,
    MAX_DISCOUNT_PERCENT,
    MAX_ITEMS,
    MAX_PRICE_CENTS,
    CoverageTag,
    HypothesisMode,
    PlanConstraints,
    SourceRef,
    Strategy,
    Verdict,
    canonical_sha256,
    input_sha256,
)

PROTOCOL_VERSION: Final = "research-protocol-v1"
MAX_PAIRS: Final = 6
MAX_CONFIRMATIONS: Final = 1
MAX_RETRIES_PER_EPISODE: Final = 2
MAX_INVOCATIONS_PER_EPISODE: Final = 2 * MAX_PAIRS + MAX_CONFIRMATIONS + MAX_RETRIES_PER_EPISODE

Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
Ident = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9._:@/+-]{1,128}$")]
Slug = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]
NonNegInt = Annotated[StrictInt, Field(ge=0)]
NonNegFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Price = Annotated[StrictInt, Field(ge=0, le=MAX_PRICE_CENTS)]
Discount = Annotated[StrictInt, Field(ge=0, le=MAX_DISCOUNT_PERCENT)]
Step = Annotated[StrictInt, Field(ge=1, le=MAX_PAIRS)]
ParamValue = (
    StrictInt | StrictBool | Annotated[str, StringConstraints(max_length=200)] | list[StrictInt]
)

#: Feature names that would leak labels or post-execution information.
FORBIDDEN_FEATURE_PREFIXES: Final = (
    "label",
    "relevance",
    "outcome",
    "observed",
    "witness",
    "expected",
    "head_value",
    "base_value",
    "confirm",
    "defect",
    "family",
    "template",
    "split",
    "verdict",
)


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, use_enum_values=False, validate_default=True
    )


class Track(StrEnum):
    A = "track_a"
    B = "track_b"


class Split(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class MeasurementMode(StrEnum):
    OFFLINE = "offline"
    RECORDED = "recorded"
    LIVE = "live"


class LabelKind(StrEnum):
    INTRODUCED_REGRESSION = "introduced_regression"
    EXISTING_DEFECT = "existing_defect"
    CLEAN = "clean"


class Outcome(StrEnum):
    """Pair outcome relative to the trusted oracle (bench/production semantics)."""

    MATCH = "match"  # usable; base == expected and head == expected
    MISMATCH = "mismatch"  # usable; base == expected and head != expected
    BASE_MISMATCH = "base_mismatch"  # base != expected (pre-existing or bad base): no update
    UNUSABLE = "unusable"  # timeout, exception, invalid output, trust or infra failure


class Confirmation(StrEnum):
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    UNUSABLE = "unusable"
    NOT_RUN = "not_run"


class Relevance(StrEnum):
    POSITIVE = "positive"
    ZERO = "zero"
    UNKNOWN = "unknown"


class CostStatus(StrEnum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Track A cases
# ---------------------------------------------------------------------------


class CheckoutInput(Model):
    prices_cents: Annotated[list[Price], Field(max_length=MAX_ITEMS)]
    discount_percent: Discount


class CaseRecord(Model):
    """Public, label-free Track A case row. Labels live in a separate ``LabelFile``."""

    schema_version: Literal["research-case-v1"]
    track: Literal[Track.A]
    case_id: Ident
    split: Split
    family_id: Slug
    defect_origin: Ident
    near_dup_cluster: Ident
    template_id: Ident
    template_version: Ident
    template_sha256: Sha256
    params: dict[Slug, ParamValue]
    params_sha256: Sha256
    contract_id: Literal["checkout-rounding@1"]
    base_source_sha256: Sha256
    head_source_sha256: Sha256
    candidate_seed: Annotated[StrictInt, Field(ge=0, le=2**32 - 1)]

    @model_validator(mode="after")
    def _params_hash(self) -> Self:
        if self.params_sha256 != canonical_sha256(self.params):
            raise ValueError("params_sha256 does not match canonical params")
        return self


def _disjoint(cases: list[CaseRecord], key: str) -> None:
    seen: dict[str, Split] = {}
    for case in cases:
        value = getattr(case, key)
        if seen.setdefault(value, case.split) is not case.split:
            raise ValueError(f"{key} {value!r} appears in more than one split")


class CaseManifest(Model):
    schema_version: Literal["research-case-manifest-v1"]
    track: Literal[Track.A]
    manifest_version: Ident
    split_policy: Literal["grouped-family-origin-cluster-v1"]
    dataset_commit: GitSha
    cases: Annotated[list[CaseRecord], Field(min_length=1)]
    cases_sha256: Sha256

    @model_validator(mode="after")
    def _grouped(self) -> Self:
        ids = [c.case_id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case_id")
        for key in ("family_id", "defect_origin", "near_dup_cluster", "template_id"):
            _disjoint(self.cases, key)
        pairs = [(c.base_source_sha256, c.head_source_sha256) for c in self.cases]
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate base/head source pair")
        if self.cases_sha256 != canonical_sha256([c.model_dump(mode="json") for c in self.cases]):
            raise ValueError("cases_sha256 does not match canonical cases")
        return self


class Witness(Model):
    input: CheckoutInput
    input_sha256: Sha256
    expected_cents: NonNegInt
    base_cents: NonNegInt
    head_cents: NonNegInt
    reproduced_by: Literal["reviewed_template_execution"]

    @model_validator(mode="after")
    def _hash(self) -> Self:
        if self.input_sha256 != input_sha256(self.input.model_dump()):
            raise ValueError("witness input_sha256 mismatch")
        return self


class CaseLabel(Model):
    case_id: Ident
    kind: LabelKind
    defect_family: Slug | None
    expected_verdict: Verdict
    witness: Witness | None
    review_rationale: Text | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        expected = {
            LabelKind.INTRODUCED_REGRESSION: Verdict.CONFIRMED_REGRESSION,
            LabelKind.EXISTING_DEFECT: Verdict.CONFIRMED_EXISTING_DEFECT,
            LabelKind.CLEAN: Verdict.NO_DEFECT_FOUND,
        }[self.kind]
        if self.expected_verdict is not expected:
            raise ValueError("expected_verdict inconsistent with kind")
        if self.kind is LabelKind.CLEAN:
            if self.witness is not None or self.defect_family is not None:
                raise ValueError("clean controls carry no witness or defect family")
            if self.review_rationale is None:
                raise ValueError("clean controls need a review rationale")
            return self
        if self.witness is None or self.defect_family is None:
            raise ValueError("defective cases need a validated witness and family")
        w = self.witness
        if w.head_cents == w.expected_cents:
            raise ValueError("witness head must differ from expected")
        if (self.kind is LabelKind.INTRODUCED_REGRESSION) != (w.base_cents == w.expected_cents):
            raise ValueError("introduced regression needs base == expected; existing defect not")
        if self.kind is LabelKind.EXISTING_DEFECT and w.base_cents != w.head_cents:
            raise ValueError("existing defect needs base == head")
        return self


class LabelFile(Model):
    """Labels for one split. The test-split file is sealed (verification lane only)."""

    schema_version: Literal["research-labels-v1"]
    split: Split
    sealed: StrictBool
    manifest_cases_sha256: Sha256
    labels: Annotated[list[CaseLabel], Field(min_length=1)]

    @model_validator(mode="after")
    def _sealed(self) -> Self:
        if (self.split is Split.TEST) != self.sealed:
            raise ValueError("exactly the test-split label file is sealed")
        ids = [x.case_id for x in self.labels]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate label case_id")
        return self


# ---------------------------------------------------------------------------
# Track B (design only: no execution in v1)
# ---------------------------------------------------------------------------


class TestCandidate(Model):
    test_id: Annotated[
        str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_./\[\]:,=+-]{1,300}$")
    ]
    origin: Literal["fail_to_pass", "pass_to_pass"]


class SweSmithTask(Model):
    schema_version: Literal["research-swe-smith-task-v1"]
    track: Literal[Track.B]
    instance_id: Ident
    dataset_id: Literal["SWE-bench/SWE-smith", "SWE-bench/SWE-smith-py"]
    dataset_revision: GitSha
    dataset_file_sha256: Sha256
    repo: Annotated[str, StringConstraints(strict=True, pattern=r"^[\w.-]+/[\w.-]+$")]
    repo_commit: GitSha
    license_spdx: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9.+-]{1,64}$")]
    license_verified: StrictBool
    redistribution: Literal["allowed", "restricted", "unknown"]
    defect_origin: Ident
    split: Split
    split_group: Ident
    candidate_tests: Annotated[list[TestCandidate], Field(min_length=1, max_length=200)]
    label_status: Literal["unverified_design_only"]
    execution_status: Literal["blocked_no_qualified_sandbox"]
    executed: Literal[False]

    @model_validator(mode="after")
    def _tests(self) -> Self:
        ids = [t.test_id for t in self.candidate_tests]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate test_id")
        if not any(t.origin == "fail_to_pass" for t in self.candidate_tests):
            raise ValueError("task needs at least one fail_to_pass test")
        return self


# ---------------------------------------------------------------------------
# Ranking observations (training data, Track A only in v1)
# ---------------------------------------------------------------------------


class CheckoutCandidate(Model):
    kind: Literal["checkout_input"]
    input_sha256: Sha256
    input: CheckoutInput
    strategy: Strategy
    is_anchor: StrictBool
    coverage_tags: list[CoverageTag]

    @model_validator(mode="after")
    def _hash(self) -> Self:
        if self.input_sha256 != input_sha256(self.input.model_dump()):
            raise ValueError("candidate input_sha256 mismatch")
        return self


class ExecutionCost(Model):
    base_invocations: NonNegInt
    head_invocations: NonNegInt
    confirm_invocations: Annotated[StrictInt, Field(ge=0, le=MAX_CONFIRMATIONS)]
    retries: Annotated[StrictInt, Field(ge=0, le=MAX_RETRIES_PER_EPISODE)]
    wall_ms: NonNegFloat


class RankingObservation(Model):
    """One executed candidate at one decision step. Untested candidates never get rows."""

    schema_version: Literal["research-ranking-observation-v1"]
    track: Literal[Track.A]
    observation_id: Ident
    case_id: Ident
    split: Literal[Split.TRAIN, Split.VALIDATION]
    collection_policy: Ident
    episode_seed: Annotated[StrictInt, Field(ge=0, le=2**32 - 1)]
    decision_step: Step
    remaining_pairs_before: Step
    pool_sha256: Sha256
    pool_size: Annotated[StrictInt, Field(ge=1, le=64)]
    candidate: CheckoutCandidate
    feature_set_version: Ident
    features: dict[Slug, StrictFloat | StrictInt | StrictBool]
    exploration: StrictBool
    selection_probability: Annotated[float, Field(gt=0, le=1)] | None
    outcome: Outcome
    unusable_reason: Slug | None
    expected_cents: NonNegInt | None
    base_cents: NonNegInt | None
    head_cents: NonNegInt | None
    confirmation: Confirmation
    relevance: Relevance
    cost: ExecutionCost
    measurement_mode: MeasurementMode

    @model_validator(mode="after")
    def _label_rules(self) -> Self:
        for name in self.features:
            if name.startswith(FORBIDDEN_FEATURE_PREFIXES):
                raise ValueError(f"feature {name!r} leaks labels or post-execution data")
        if self.exploration and self.selection_probability is None:
            raise ValueError("exploration rows must record selection_probability")
        if self.decision_step + self.remaining_pairs_before != MAX_PAIRS + 1:
            raise ValueError("remaining_pairs_before inconsistent with decision_step")
        usable = self.outcome is not Outcome.UNUSABLE
        if usable == (self.unusable_reason is not None):
            raise ValueError("unusable_reason required exactly for unusable outcomes")
        if usable and None in (self.expected_cents, self.base_cents, self.head_cents):
            raise ValueError("usable outcomes record expected/base/head values")
        if usable:
            base_ok = self.base_cents == self.expected_cents
            head_ok = self.head_cents == self.expected_cents
            derived = (
                Outcome.BASE_MISMATCH
                if not base_ok
                else Outcome.MATCH
                if head_ok
                else Outcome.MISMATCH
            )
            if derived is not self.outcome:
                raise ValueError("outcome inconsistent with recorded values")
        if (self.outcome is Outcome.MISMATCH) == (self.confirmation is Confirmation.NOT_RUN):
            raise ValueError("confirmation runs exactly after a usable head mismatch")
        relevance = (
            Relevance.POSITIVE
            if self.outcome is Outcome.MISMATCH and self.confirmation is Confirmation.REPRODUCED
            else Relevance.ZERO
            if self.outcome is Outcome.MATCH
            else Relevance.UNKNOWN
        )
        if relevance is not self.relevance:
            raise ValueError(f"relevance must be {relevance.value} for this outcome")
        return self


# ---------------------------------------------------------------------------
# Generator examples
# ---------------------------------------------------------------------------


class ContextFile(Model):
    side: Literal["base", "head"]
    path: Literal["checkout.py"]
    sha256: Sha256
    content: Annotated[str, StringConstraints(strict=True, max_length=64 * 1024)]


class GeneratorInput(Model):
    prompt_format_version: Ident
    contract_id: Literal["checkout-rounding@1"]
    behavior_spec_sha256: Sha256
    allowed_test_interface: Literal["checkout.total_cents(prices_cents, discount_percent)"]
    context_files: Annotated[list[ContextFile], Field(min_length=2, max_length=2)]
    unified_diff: Annotated[str, StringConstraints(strict=True, max_length=32 * 1024)]


class TargetHypothesis(Model):
    mode: HypothesisMode
    rationale: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    source_refs: Annotated[list[SourceRef], Field(max_length=4)]
    suggested_strategy: Strategy
    constraints: PlanConstraints
    triggering_conditions: Text
    grounding_witness_sha256: Sha256 | None

    @model_validator(mode="after")
    def _grounded(self) -> Self:
        if self.mode is not HypothesisMode.OTHER and self.grounding_witness_sha256 is None:
            raise ValueError("modeled hypotheses must be grounded in a reproduced witness")
        return self


class TeacherRef(Model):
    model_id: Ident
    provider: Ident
    validated_against_execution: Literal[True]
    reviewed: StrictBool


class GeneratorExample(Model):
    schema_version: Literal["research-generator-example-v1"]
    example_id: Ident
    case_id: Ident
    split: Literal[Split.TRAIN, Split.VALIDATION]
    input: GeneratorInput
    target: Annotated[list[TargetHypothesis], Field(min_length=1, max_length=6)]
    teacher: TeacherRef | None
    contains_template_params: Literal[False]
    contains_fix_patch: Literal[False]

    @model_validator(mode="after")
    def _unique_modes(self) -> Self:
        modes = [h.mode for h in self.target]
        if len(set(modes)) != len(modes):
            raise ValueError("target hypotheses must be deduplicated by mode")
        sides = sorted(f.side for f in self.input.context_files)
        if sides != ["base", "head"]:
            raise ValueError("context needs exactly one base and one head file")
        return self


# ---------------------------------------------------------------------------
# Raw evaluation results
# ---------------------------------------------------------------------------


class ProbeSummary(Model):
    step: Step
    candidate_input_sha256: Sha256
    outcome: Outcome
    confirmation: Confirmation
    retries: Annotated[StrictInt, Field(ge=0, le=MAX_RETRIES_PER_EPISODE)]


class Invocations(Model):
    base: NonNegInt
    head: NonNegInt
    confirm: Annotated[StrictInt, Field(ge=0, le=MAX_CONFIRMATIONS)]
    failed: NonNegInt

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if self.base + self.head + self.confirm > MAX_INVOCATIONS_PER_EPISODE:
            raise ValueError("invocations exceed the frozen episode bound")
        return self


class Timings(Model):
    wall_ms: NonNegFloat
    queue_ms: NonNegFloat | None
    generation_ms: NonNegFloat | None
    feature_ms: NonNegFloat | None
    ranking_ms: NonNegFloat | None
    sandbox_startup_ms: NonNegFloat | None
    execution_ms: NonNegFloat | None
    confirmation_ms: NonNegFloat | None
    cleanup_ms: NonNegFloat | None
    cold_start: StrictBool | None


class TokenUsage(Model):
    input_tokens: NonNegInt | None
    output_tokens: NonNegInt | None
    reasoning_tokens: NonNegInt | None


class ModelRefs(Model):
    generator_model_id: Ident | None
    generator_revision: Ident | None
    adapter_sha256: Sha256 | None
    ranker_checkpoint_sha256: Sha256 | None
    frontier_model_id: Ident | None
    prompt_sha256: Sha256 | None


class RawResultRow(Model):
    """One (case, arm, seed, repetition) evaluation episode. Carries no labels."""

    schema_version: Literal["research-raw-result-v1"]
    protocol_version: Literal["research-protocol-v1"]
    run_id: Ident
    implementation_sha: GitSha
    track: Literal[Track.A]
    case_id: Ident
    split: Split
    arm_id: Ident
    seed: Annotated[StrictInt, Field(ge=0, le=2**32 - 1)]
    repetition: NonNegInt
    measurement_mode: MeasurementMode
    snap_routing: Literal["disabled"]
    pool_sha256: Sha256 | None
    models: ModelRefs
    probes: Annotated[list[ProbeSummary], Field(max_length=MAX_PAIRS)]
    invocations: Invocations
    verdict: Verdict | Literal["failed"]
    reason_code: Slug
    pairs_to_first_confirmed_witness: Step | None
    censored: StrictBool
    timings: Timings
    tokens: TokenUsage
    cost_usd: NonNegFloat | None
    cost_status: CostStatus

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if [p.step for p in self.probes] != list(range(1, len(self.probes) + 1)):
            raise ValueError("probe steps must be 1..n")
        if sum(p.retries for p in self.probes) > MAX_RETRIES_PER_EPISODE:
            raise ValueError("retries exceed the frozen episode bound")
        first = next(
            (
                p.step
                for p in self.probes
                if p.outcome is Outcome.MISMATCH and p.confirmation is Confirmation.REPRODUCED
            ),
            None,
        )
        if first != self.pairs_to_first_confirmed_witness:
            raise ValueError("pairs_to_first_confirmed_witness inconsistent with probes")
        if self.censored != (first is None):
            raise ValueError("censored must be true exactly when no confirmed witness")
        if (self.cost_usd is None) != (self.cost_status is CostStatus.UNKNOWN):
            raise ValueError("cost_usd is null exactly when cost_status is unknown")
        return self


# ---------------------------------------------------------------------------
# Cost/timing ledger and spend policy
# ---------------------------------------------------------------------------


class LedgerCategory(StrEnum):
    DATA_COLLECTION = "data_collection"
    TRAINING_GPU = "training_gpu"
    MODEL_INFERENCE = "model_inference"
    EXECUTION_INFRA = "execution_infra"
    STORAGE_IDLE = "storage_idle"
    LABELING = "labeling"


class LedgerPhase(StrEnum):
    PILOT = "pilot"
    TRAINING = "training"
    EVALUATION = "evaluation"
    CONTINGENCY = "contingency"


class LedgerEntry(Model):
    schema_version: Literal["research-ledger-v1"]
    entry_id: Ident
    recorded_at: datetime
    lane: Literal[
        "dataset", "ranker", "generator", "baselines", "measurement", "integration", "verification"
    ]
    provider: Ident
    category: LedgerCategory
    phase: LedgerPhase
    resource_id: Ident | None
    run_id: Ident | None
    case_id: Ident | None
    quantity: NonNegFloat
    unit: Ident
    unit_price_usd: NonNegFloat | None
    cost_usd: NonNegFloat | None
    cost_status: CostStatus
    pricing_reference: Annotated[str, StringConstraints(strict=True, max_length=500)] | None
    pricing_date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None
    wall_ms: NonNegFloat | None
    queue_ms: NonNegFloat | None
    measurement_mode: MeasurementMode
    paid: StrictBool
    estimated_job_usd: NonNegFloat | None
    coordinator_checkin_ref: Ident | None
    teardown_verified: StrictBool | None

    @model_validator(mode="after")
    def _paid_rules(self) -> Self:
        offset = self.recorded_at.utcoffset()
        if offset is None or offset.total_seconds():
            raise ValueError("recorded_at must be UTC")
        if (self.cost_usd is None) != (self.cost_status is CostStatus.UNKNOWN):
            raise ValueError("cost_usd is null exactly when cost_status is unknown")
        if self.paid:
            if self.pricing_reference is None or self.pricing_date is None:
                raise ValueError("paid entries need a pricing reference and date")
            if self.estimated_job_usd is None:
                raise ValueError("paid entries need the job's pre-launch estimate")
            if (
                self.estimated_job_usd > SINGLE_JOB_CHECKIN_USD
                and self.coordinator_checkin_ref is None
            ):
                raise ValueError("jobs estimated above the check-in threshold need a checkin ref")
            if self.resource_id is not None and self.teardown_verified is None:
                raise ValueError("paid provisioned resources must record teardown verification")
        return self


SINGLE_JOB_CHECKIN_USD: Final = 50.0


class ProviderGuard(Model):
    provider: Ident
    keys: list[Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]]
    use: Text
    hard_stop: Text
    idle_shutdown_minutes: Annotated[StrictInt, Field(ge=1, le=120)] | None
    teardown_check: Text


class SpendPolicy(Model):
    schema_version: Literal["research-spend-policy-v1"]
    status: Literal["user_approved_uncapped"]
    approval_quote: Text
    total_cap_usd: None
    single_job_checkin_usd: Annotated[StrictFloat, Field(ge=50.0, le=50.0)]
    mandatory_guards: Annotated[list[Text], Field(min_length=3)]
    superseded_proposal: dict[Slug, StrictFloat | StrictInt]
    providers: Annotated[list[ProviderGuard], Field(min_length=1)]


# ---------------------------------------------------------------------------
# Frozen protocol
# ---------------------------------------------------------------------------


class ArmKind(StrEnum):
    CONTROL = "control"
    FACTORIAL = "factorial"
    SYSTEM_BASELINE = "system_baseline"
    FRONTIER = "frontier"


class ArmSpec(Model):
    arm_id: Slug
    kind: ArmKind
    generator: Slug | None
    selector: Slug
    executes: StrictBool
    feedback: Literal["none", "pair_observation"]
    candidate_freedom: Literal["shared_pool", "free_input_documented"]
    model_status: Literal["not_applicable", "pinned_by_lane", "frozen_by_baselines_lane"]
    claim_scope: Text


class SplitTarget(Model):
    defect_cases: NonNegInt
    clean_controls: NonNegInt
    min_families: Annotated[StrictInt, Field(ge=1)]


class ProtocolV1(Model):
    schema_version: Literal["research-protocol-v1"]
    starting_sha: GitSha
    verified_candidate_sha: GitSha
    production_lock_sha256: dict[Literal["pyproject.toml", "uv.lock", "bench/policy.json"], Sha256]
    contract_id: Literal["checkout-rounding@1"]
    max_pairs: Literal[6]
    max_confirmations: Literal[1]
    max_retries_per_episode: Literal[2]
    max_invocations_per_episode: Literal[15]
    invocation_timeout_seconds: Literal[5]
    required_clean_coverage: list[CoverageTag]
    candidate_pool: dict[Slug, Any]
    snap_routing: Literal["disabled"]
    master_seed: StrictInt
    replicate_seeds: Annotated[list[StrictInt], Field(min_length=5, max_length=5)]
    bootstrap: dict[Slug, StrictInt | str]
    budgets_reported: list[Literal[1, 3, 6]]
    split_targets: dict[Literal["train", "validation", "test"], SplitTarget]
    arms: Annotated[list[ArmSpec], Field(min_length=1)]
    track_b_status: Literal["design_only_blocked_no_qualified_sandbox"]

    @model_validator(mode="after")
    def _arms(self) -> Self:
        ids = [a.arm_id for a in self.arms]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate arm_id")
        if self.contract_id != CONTRACT_ID:
            raise ValueError("contract mismatch")
        return self


SCHEMA_MODELS: Final[dict[str, type[BaseModel]]] = {
    "CaseRecord": CaseRecord,
    "CaseManifest": CaseManifest,
    "CaseLabel": CaseLabel,
    "LabelFile": LabelFile,
    "SweSmithTask": SweSmithTask,
    "RankingObservation": RankingObservation,
    "GeneratorExample": GeneratorExample,
    "RawResultRow": RawResultRow,
    "LedgerEntry": LedgerEntry,
    "SpendPolicy": SpendPolicy,
    "ProtocolV1": ProtocolV1,
}
