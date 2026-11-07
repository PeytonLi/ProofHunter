"""Reference math, oracle and verdict helpers (fixture implementations).

These are executable statements of PRD sections 4 and 5 for tests and for the
fixture adapters. Production code lives in ``proofhunter.bayes`` and
``proofhunter.oracle``; owners must not import this module from ``src/``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence

from proofhunter.contracts import (
    COMMON_ANCHORS,
    DEMO_SENTINEL,
    GENERATOR_VERSION,
    LIKELIHOOD_POLICY,
    MANDATORY_HYPOTHESES,
    ORACLE_VERSION,
    PRIOR_POLICY,
    PRIOR_WEIGHTS_V1,
    Q_H0,
    Q_OTHER,
    Q_PREDICTOR_AGREES,
    Q_PREDICTOR_DIFFERS,
    REQUIRED_COVERAGE_TAGS,
    VERDICT_WORDING,
    Belief,
    BinaryOutcome,
    Candidate,
    CandidateBank,
    CandidateInput,
    ConfirmationStatus,
    Coverage,
    CoverageTag,
    Enrollment,
    EvidenceKind,
    ExclusionReason,
    HypothesisId,
    HypothesisPlan,
    HypothesisSource,
    HypothesisSummary,
    InitialBelief,
    ObservationOutcome,
    OracleResult,
    OracleStatus,
    PlanSource,
    ProbeRecord,
    ProbeSelection,
    ProviderMode,
    ReasonCode,
    ReceiptV1,
    Replay,
    RetainedHypothesis,
    SandboxObservation,
    SelectionPolicy,
    SelectionReason,
    Strategy,
    TrustTier,
    Verdict,
    VerdictResult,
    builtin_hypothesis_payload,
    compute_receipt_sha256,
    input_sha256,
    pool_sha256,
    verify_receipt_sha256,
)

Prices = Sequence[int]

# ---------------------------------------------------------------- oracle (T1)


def oracle_expected(prices: Prices, discount: int) -> int:
    """Enrolled ``checkout-rounding@1`` rule: round the discounted subtotal once, half up."""
    n = sum(prices) * (100 - discount)
    return (n + 50) // 100


def evaluate(enrollment: Enrollment, candidate: Candidate) -> OracleResult:
    return OracleResult(
        contract_id=enrollment.contract_id, contract_sha256=enrollment.contract_sha256,
        oracle_version=ORACLE_VERSION, input_sha256=candidate.input_sha256,
        trust_tier=TrustTier.T1,
        expected_cents=oracle_expected(candidate.prices_cents, candidate.discount_percent),
        status=OracleStatus.OK,
    )


# ---------------------------------------------------------------- predictors


def predict_h0(prices: Prices, discount: int) -> int:
    return (sum(prices) * (100 - discount) + 50) // 100


def predict_per_item(prices: Prices, discount: int) -> int:
    return sum((p * (100 - discount) + 50) // 100 for p in prices)


def predict_half_even(prices: Prices, discount: int) -> int:
    quotient, remainder = divmod(sum(prices) * (100 - discount), 100)
    if remainder > 50 or (remainder == 50 and quotient % 2 == 1):
        quotient += 1
    return quotient


PREDICTORS: Mapping[HypothesisId, Callable[[Prices, int], int]] = {
    HypothesisId.H0: predict_h0,
    HypothesisId.PER_ITEM_ROUNDING: predict_per_item,
    HypothesisId.HALF_EVEN_ROUNDING: predict_half_even,
}


def predictions(ids: Iterable[HypothesisId], prices: Prices,
                discount: int) -> dict[HypothesisId, int | None]:
    return {h: (PREDICTORS[h](prices, discount) if h in PREDICTORS else None) for h in ids}


def likelihoods(ids: Iterable[HypothesisId], prices: Prices,
                discount: int) -> dict[HypothesisId, float]:
    """``rounding-likelihood-v1``: q_h(x) = P(mismatch | h, x)."""
    h0 = predict_h0(prices, discount)
    out: dict[HypothesisId, float] = {}
    for h in ids:
        if h is HypothesisId.H0:
            out[h] = Q_H0
        elif h is HypothesisId.OTHER:
            out[h] = Q_OTHER
        else:
            differs = PREDICTORS[h](prices, discount) != h0
            out[h] = Q_PREDICTOR_DIFFERS if differs else Q_PREDICTOR_AGREES
    return out


# ---------------------------------------------------------------- information


def entropy(p: Mapping[HypothesisId, float]) -> float:
    return -math.fsum(v * math.log2(v) for v in p.values() if v > 0)


def posterior(prior: Mapping[HypothesisId, float], q: Mapping[HypothesisId, float],
              outcome: BinaryOutcome) -> dict[HypothesisId, float]:
    like = {h: (q[h] if outcome is BinaryOutcome.MISMATCH else 1 - q[h]) for h in prior}
    evidence = math.fsum(like[h] * prior[h] for h in prior)
    return {h: like[h] * prior[h] / evidence for h in prior}


def p_mismatch(prior: Mapping[HypothesisId, float], q: Mapping[HypothesisId, float]) -> float:
    return math.fsum(q[h] * prior[h] for h in prior)


def eig(prior: Mapping[HypothesisId, float], q: Mapping[HypothesisId, float]) -> float:
    pm = p_mismatch(prior, q)
    value = entropy(prior) - pm * entropy(posterior(prior, q, BinaryOutcome.MISMATCH)) - (
        1 - pm) * entropy(posterior(prior, q, BinaryOutcome.MATCH))
    if value < 0:
        if value < -1e-12:
            raise ArithmeticError("negative EIG beyond tolerance")
        value = 0.0
    return value


def _normalized(values: Mapping[HypothesisId, float]) -> dict[HypothesisId, float]:
    total = math.fsum(values.values())
    rounded = {h: round(v / total, 12) for h, v in values.items()}
    return rounded


# ---------------------------------------------------------------- coverage and candidates


def coverage_tags(prices: Prices, discount: int) -> list[CoverageTag]:
    tags: set[CoverageTag] = set()
    if not prices:
        tags.add(CoverageTag.EMPTY)
    elif len(prices) == 1:
        tags.add(CoverageTag.SINGLETON)
    else:
        tags.add(CoverageTag.MULTI_ITEM)
    if discount == 0:
        tags.add(CoverageTag.ZERO_DISCOUNT)
    if discount == 100:
        tags.add(CoverageTag.FULL_DISCOUNT)
    if sum(prices) * (100 - discount) % 100 == 50:
        tags.add(CoverageTag.HALF_CENT)
    if len(prices) == 8:
        tags.add(CoverageTag.MAX_ITEMS)
    if any(p == 10_000 for p in prices):
        tags.add(CoverageTag.MAX_PRICE)
    return sorted(tags)


def make_candidate(prices: Prices, discount: int, strategy: Strategy, *,
                   is_anchor: bool = False) -> Candidate:
    data = CandidateInput(prices_cents=list(prices), discount_percent=discount)
    return Candidate(prices_cents=list(prices), discount_percent=discount,
                     input_sha256=input_sha256(data), strategy=strategy, is_anchor=is_anchor,
                     coverage_tags=coverage_tags(prices, discount),
                     generator_version=GENERATOR_VERSION)


def _both_predictors_differ(prices: Prices, discount: int) -> bool:
    h0 = predict_h0(prices, discount)
    return predict_per_item(prices, discount) != h0 and predict_half_even(prices, discount) != h0


_BOUNDARY_INPUTS: tuple[tuple[tuple[int, ...], int], ...] = (
    ((0,), 0), ((10_000,), 0), ((10_000,) * 8, 0), ((10_000,) * 8, 100), ((1,), 50),
    ((1,), 1), ((1,), 99), ((5,), 10), ((50,), 1), ((10_000,), 99), ((0,) * 8, 50),
    ((1, 1), 50), ((3,), 50), ((7,), 50), ((10_000, 1), 50), ((25,), 2), ((1,), 100),
    ((9_999,), 50), ((2,), 25), ((45,), 10),
)
_CONSTRAINT_INPUTS: tuple[tuple[tuple[int, ...], int], ...] = (
    ((5, 5), 10), ((15, 15), 10), ((25, 25), 10), ((5, 5, 5), 10), ((35, 35), 10),
    ((45,), 10), ((5,), 30), ((15,), 10), ((25,), 10), ((35,), 10), ((5, 5, 5, 5), 10),
    ((1, 1, 1), 50), ((3, 3), 50), ((7, 7), 50), ((15, 15, 15, 15), 10), ((65, 65), 10),
    ((105,), 10), ((95,), 10), ((85, 85), 10), ((75,), 10),
)


def build_bank(seed: int, *, generator_version: str = GENERATOR_VERSION) -> CandidateBank:
    """Pinned offline fixture bank. Only the sentinel has both predictors differing
    from H0, so it is the unique highest-EIG input at the first step."""
    import random

    chosen: dict[str, Candidate] = {}

    def add(prices: Prices, discount: int, strategy: Strategy, *, anchor: bool = False,
            cap: int = 20) -> None:
        candidate = make_candidate(prices, discount, strategy, is_anchor=anchor)
        if candidate.input_sha256 in chosen:
            return
        if not anchor and _both_predictors_differ(prices, discount):
            return
        if not anchor and sum(1 for c in chosen.values()
                              if c.strategy is strategy and not c.is_anchor) >= cap:
            return
        chosen[candidate.input_sha256] = candidate

    for prices, discount in COMMON_ANCHORS:
        strategy = Strategy.CONSTRAINT if (prices, discount) == DEMO_SENTINEL else (
            Strategy.BOUNDARY)
        add(prices, discount, strategy, anchor=True)
    for prices, discount in _BOUNDARY_INPUTS:
        add(prices, discount, Strategy.BOUNDARY)
    for prices, discount in _CONSTRAINT_INPUTS:
        add(prices, discount, Strategy.CONSTRAINT)
    rng = random.Random(f"{generator_version}:seeded_fuzz:{seed}")  # noqa: S311 - not crypto
    for _ in range(400):
        size = rng.choice((0, 1, 1, 2, 2, 2, 3, 3, 4, 5, 8))
        prices = tuple(rng.choice((rng.randint(0, 99), rng.randint(0, 10_000)))
                       for _ in range(size))
        add(prices, rng.randint(0, 100), Strategy.SEEDED_FUZZ)
    candidates = list(chosen.values())[:64]
    return CandidateBank(generator_version=generator_version, seed=seed, candidates=candidates,
                         bank_sha256=pool_sha256(candidates))


def generate(plan: HypothesisPlan, seed: int) -> CandidateBank:
    del plan
    return build_bank(seed)


# ---------------------------------------------------------------- priors, selection, update


def initialize(plan: HypothesisPlan) -> InitialBelief:
    source = PlanSource.OFFLINE_FIXTURE if plan.provenance.mode is ProviderMode.OFFLINE_FIXTURE \
        else PlanSource.META
    if plan.provenance.mode is ProviderMode.FALLBACK:
        source = PlanSource.BUILTIN_FALLBACK
    proposals = {p.mode.value: p for p in plan.hypotheses}
    rejected = len(plan.hypotheses) - len(proposals)
    if set(proposals) <= {HypothesisId.OTHER.value}:
        proposals = {p.mode.value: p for p in builtin_hypothesis_payload().hypotheses}
        source = PlanSource.BUILTIN_RESTORED if source is not PlanSource.BUILTIN_FALLBACK \
            else source
    retained: list[RetainedHypothesis] = [RetainedHypothesis(
        id=HypothesisId.H0, source=HypothesisSource.MANDATORY, rationale=None,
        suggested_strategy=None, constraints=None, raw_prior_weight=PRIOR_WEIGHTS_V1["H0"])]
    for hid in (HypothesisId.PER_ITEM_ROUNDING, HypothesisId.HALF_EVEN_ROUNDING,
                HypothesisId.OTHER):
        proposal = proposals.get(hid.value)
        if proposal is None and hid not in MANDATORY_HYPOTHESES:
            continue
        retained.append(RetainedHypothesis(
            id=hid,
            source=(HypothesisSource.MANDATORY if proposal is None else
                    HypothesisSource.META if source in (PlanSource.META,
                                                        PlanSource.OFFLINE_FIXTURE)
                    else HypothesisSource.BUILTIN),
            rationale=None if proposal is None else proposal.rationale,
            suggested_strategy=None if proposal is None else proposal.suggested_strategy,
            constraints=None if proposal is None else proposal.constraints,
            raw_prior_weight=PRIOR_WEIGHTS_V1[hid.value]))
    prior = _normalized({h.id: h.raw_prior_weight for h in retained})
    summary = HypothesisSummary(plan_source=source, prior_policy=PRIOR_POLICY, retained=retained,
                                initial_prior=prior, rejected_proposals=rejected)
    return InitialBelief(belief=Belief(probabilities=prior, prior_policy=PRIOR_POLICY,
                                       likelihood_policy=LIKELIHOOD_POLICY, updates=0),
                         hypotheses=summary)


def score(belief: Belief, candidate: Candidate) -> float:
    q = likelihoods(belief.probabilities, candidate.prices_cents, candidate.discount_percent)
    return eig(belief.probabilities, q)


def select(belief: Belief, candidates: Sequence[Candidate],
           policy: SelectionPolicy) -> ProbeSelection:
    if not candidates:
        raise ValueError("no candidates")
    pool = list(candidates)
    reason = SelectionReason.MAX_EIG
    missing = set(policy.missing_coverage)
    if missing and policy.remaining_pairs <= len(missing):
        covering = [c for c in pool if missing & set(c.coverage_tags)]
        if covering:
            pool, reason = covering, SelectionReason.COVERAGE_GUARD
    ranked = sorted(pool, key=lambda c: (-round(score(belief, c), policy.eig_round_decimals),
                                         c.input_sha256))
    best = ranked[0]
    q = likelihoods(belief.probabilities, best.prices_cents, best.discount_percent)
    return ProbeSelection(
        candidate=best, candidate_pool_sha256=pool_sha256(candidates), pool_size=len(candidates),
        eig_bits=score(belief, best), p_mismatch=p_mismatch(belief.probabilities, q),
        prior=belief.probabilities, likelihood_mismatch=q,
        predictions=predictions(belief.probabilities, best.prices_cents, best.discount_percent),
        reason=reason)


def update(belief: Belief, selection: ProbeSelection, observed: BinaryOutcome) -> Belief:
    post = _normalized(posterior(belief.probabilities, selection.likelihood_mismatch, observed))
    return Belief(probabilities=post, prior_policy=belief.prior_policy,
                  likelihood_policy=belief.likelihood_policy, updates=belief.updates + 1)


# ---------------------------------------------------------------- evidence and verdicts


def classify_pair(oracle: OracleResult, base: SandboxObservation, head: SandboxObservation
                  ) -> tuple[EvidenceKind, ExclusionReason | None]:
    if oracle.status is not OracleStatus.OK or oracle.trust_tier is not TrustTier.T1:
        return EvidenceKind.EXCLUDED, ExclusionReason.ORACLE_UNAVAILABLE
    for obs in (base, head):
        if obs.outcome is not ObservationOutcome.OK:
            return EvidenceKind.EXCLUDED, {
                ObservationOutcome.TIMEOUT: ExclusionReason.TIMEOUT,
                ObservationOutcome.NONZERO: ExclusionReason.NONZERO_EXIT,
                ObservationOutcome.INVALID: ExclusionReason.INVALID_OUTPUT,
                ObservationOutcome.DENIED: ExclusionReason.DENIED,
            }.get(obs.outcome, ExclusionReason.INFRA_ERROR)
    expected = oracle.expected_cents
    if base.value != expected:
        kind = EvidenceKind.SHARED_MISMATCH if head.value == base.value else (
            EvidenceKind.BASE_MISMATCH)
        return kind, ExclusionReason.BASE_MISMATCH
    if head.value == expected:
        return EvidenceKind.MATCH, None
    return EvidenceKind.HEAD_MISMATCH, None


def confirmation_status(head: SandboxObservation,
                        confirmation: SandboxObservation | None) -> ConfirmationStatus:
    if confirmation is None:
        return ConfirmationStatus.NOT_ATTEMPTED
    if confirmation.outcome is not ObservationOutcome.OK:
        return ConfirmationStatus.UNUSABLE
    return (ConfirmationStatus.REPRODUCED if confirmation.value == head.value
            else ConfirmationStatus.DISAGREED)


def compute_coverage(probes: Sequence[ProbeRecord], coverage_guard_used: bool) -> Coverage:
    observed: set[CoverageTag] = set()
    usable: set[str] = set()
    for probe in probes:
        if probe.evidence_kind is EvidenceKind.MATCH:
            observed.update(probe.candidate.coverage_tags)
            usable.add(probe.candidate.input_sha256)
    required = sorted(REQUIRED_COVERAGE_TAGS)
    observed_required = sorted(observed & set(required))
    return Coverage(required=required, observed=sorted(observed),
                    missing=sorted(set(required) - set(observed_required)),
                    distinct_usable_pairs=len(usable), coverage_guard_used=coverage_guard_used)


def _result(verdict: Verdict, reason: ReasonCode, probe_no: int | None = None,
            next_requirement: str | None = None) -> VerdictResult:
    return VerdictResult(verdict=verdict, reason_code=reason, counterexample_probe_no=probe_no,
                         wording=VERDICT_WORDING[verdict], next_requirement=next_requirement)


def classify(probes: Sequence[ProbeRecord], coverage: Coverage,
             stop_reason: ReasonCode) -> VerdictResult:
    for probe in probes:
        status = probe.confirmation_status
        if status is not ConfirmationStatus.REPRODUCED:
            continue
        if probe.evidence_kind is EvidenceKind.HEAD_MISMATCH:
            return _result(Verdict.CONFIRMED_REGRESSION, ReasonCode.CONFIRMED_EVIDENCE,
                           probe.probe_no)
        if probe.evidence_kind is EvidenceKind.SHARED_MISMATCH:
            return _result(Verdict.CONFIRMED_EXISTING_DEFECT, ReasonCode.CONFIRMED_EVIDENCE,
                           probe.probe_no)
    if any(p.confirmation_status is ConfirmationStatus.DISAGREED for p in probes):
        return _result(Verdict.INCONCLUSIVE, ReasonCode.NONDETERMINISTIC,
                       next_requirement="A fresh head replay must reproduce the mismatch.")
    if any(p.evidence_kind is EvidenceKind.BASE_MISMATCH for p in probes):
        return _result(Verdict.INCONCLUSIVE, ReasonCode.PREEXISTING_MISMATCH_CHANGED,
                       next_requirement="Base and head disagree with each other and the oracle.")
    if any(p.exclusion_reason is ExclusionReason.ORACLE_UNAVAILABLE for p in probes) or (
            stop_reason is ReasonCode.ORACLE_UNAVAILABLE):
        return _result(Verdict.INCONCLUSIVE, ReasonCode.ORACLE_UNAVAILABLE,
                       next_requirement="A trusted T1 oracle is required.")
    unresolved = any(p.evidence_kind is not EvidenceKind.MATCH for p in probes)
    if stop_reason in (ReasonCode.ISOLATION_UNVERIFIED, ReasonCode.PROVIDER_UNAVAILABLE,
                       ReasonCode.TRUST_FAILURE, ReasonCode.REVISION_UNAVAILABLE,
                       ReasonCode.UNSUPPORTED_CONTRACT, ReasonCode.SOURCE_INVALID,
                       ReasonCode.DEADLINE_EXCEEDED):
        return _result(Verdict.INCONCLUSIVE, stop_reason,
                       next_requirement="Resolve the blocking dependency and rerun.")
    if not unresolved and coverage.complete and coverage.distinct_usable_pairs >= 3:
        return _result(Verdict.NO_DEFECT_FOUND, ReasonCode.COVERAGE_COMPLETE)
    if unresolved:
        return _result(Verdict.INCONCLUSIVE, ReasonCode.NONDETERMINISTIC if any(
            p.evidence_kind is EvidenceKind.HEAD_MISMATCH for p in probes)
            else ReasonCode.COVERAGE_INSUFFICIENT,
            next_requirement="Every probe must be a usable paired match.")
    return _result(Verdict.INCONCLUSIVE, ReasonCode.COVERAGE_INSUFFICIENT,
                   next_requirement=f"Missing coverage: {', '.join(coverage.missing) or 'pairs'}.")


def seal(receipt: ReceiptV1) -> ReceiptV1:
    unsealed = receipt.model_copy(update={"receipt_sha256": None})
    return ReceiptV1.model_validate(
        {**unsealed.model_dump(), "receipt_sha256": compute_receipt_sha256(unsealed)})


def verify_receipt(receipt_json: bytes | str) -> ReceiptV1:
    from proofhunter.ports import AdapterError

    if not verify_receipt_sha256(receipt_json):
        raise AdapterError("oracle", ReasonCode.RECEIPT_TAMPERED,
                           "receipt hash does not verify", retryable=False)
    return ReceiptV1.model_validate_json(receipt_json)


def validate_replay(receipt: ReceiptV1, probe_no: int | None) -> Replay:
    from proofhunter.ports import AdapterError

    wanted = receipt.counterexample_probe_no if probe_no is None else probe_no
    if receipt.replay is None or wanted is None or receipt.replay.probe_no != wanted:
        raise AdapterError("oracle", ReasonCode.REPLAY_UNAVAILABLE, "no eligible pinned input",
                           retryable=False)
    probe = receipt.probes[wanted - 1]
    replay = receipt.replay
    if (probe.candidate.input_sha256 != replay.input_sha256
            or probe.base.source_sha256 != replay.base_source_sha256
            or probe.head.source_sha256 != replay.head_source_sha256
            or probe.head.harness_sha256 != replay.harness_sha256
            or probe.head.image_digest != replay.image_digest):
        raise AdapterError("oracle", ReasonCode.TRUST_FAILURE, "replay provenance mismatch",
                           retryable=False)
    return replay


def dumps(data: object) -> str:
    """Pretty, stable JSON text used for committed fixture files."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
