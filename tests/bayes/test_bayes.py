import json
import math
from collections import Counter

import pytest

from fixtures import catalog, reference, simulate
from proofhunter import bayes
from proofhunter import contracts as c


def plan():
    return c.HypothesisPlan(
        hypotheses=c.builtin_hypothesis_payload().hypotheses,
        provenance=c.ProviderProvenance(provider=c.ProviderName.META,
            mode=c.ProviderMode.OFFLINE_FIXTURE, model_id=None, request_id=None,
            latency_ms=None, attempts=0, reason_code=None))


def policy(remaining=6, missing=()):
    return c.SelectionPolicy(remaining_pairs=remaining, missing_coverage=sorted(missing))


def pinned():
    return c.CandidateBank.model_validate_json(
        (catalog.DEMO_DIR / 'candidate_bank.seed17.json').read_text())


def test_reference_vector_and_chain():
    initial = bayes.initialize(plan())
    assert initial == reference.initialize(plan())
    belief = initial.belief
    candidate = bayes.make_candidate(*c.DEMO_SENTINEL, c.Strategy.CONSTRAINT)
    selection = bayes.select(belief, [candidate], policy())
    assert list(selection.likelihood_mismatch.values()) == [.02, .98, .98, .20]
    assert selection.p_mismatch == pytest.approx(.326)
    updated = bayes.update(belief, selection, c.BinaryOutcome.MISMATCH)
    assert list(updated.probabilities.values()) == pytest.approx(
        [.03681, .45092, .45092, .06135], abs=1e-5)
    for outcome in [c.BinaryOutcome.MATCH, c.BinaryOutcome.MISMATCH] * 7:
        selection = bayes.select(belief, [candidate], policy())
        assert selection.eig_bits == reference.select(belief, [candidate], policy()).eig_bits
        belief = bayes.update(belief, selection, outcome)
        assert math.fsum(belief.probabilities.values()) == pytest.approx(1, abs=1e-11)


def test_initialize_first_dedupe_restore_and_subset():
    original = plan()
    first = original.hypotheses[0]
    duplicate = first.model_copy(update={'rationale': 'second'})
    result = bayes.initialize(original.model_copy(update={'hypotheses': [first, duplicate]}))
    assert result.hypotheses.rejected_proposals == 1
    assert result.hypotheses.retained[1].rationale == first.rationale
    assert result.hypotheses.retained[-1].source is c.HypothesisSource.MANDATORY
    assert math.fsum(result.belief.probabilities.values()) == pytest.approx(1)
    for proposals in ([], [original.hypotheses[-1]]):
        restored = bayes.initialize(original.model_copy(update={'hypotheses': proposals}))
        assert restored.hypotheses.plan_source is c.PlanSource.BUILTIN_RESTORED
    fallback = original.model_copy(update={'provenance': original.provenance.model_copy(
        update={'mode': c.ProviderMode.FALLBACK})})
    assert bayes.initialize(fallback) == reference.initialize(fallback)


@pytest.mark.parametrize('seed', [True, False, -1, c.MAX_SEED + 1, 1.0, '17', None])
def test_bad_seeds(seed):
    with pytest.raises(ValueError):
        bayes.generate(plan(), seed)


def test_generation_and_tags():
    bank = bayes.generate(plan(), 17)
    assert bank == bayes.generate(plan(), 17)
    assert bank != bayes.generate(plan(), 18)
    assert len(bank.candidates) <= 64
    assert len({x.input_sha256 for x in bank.candidates}) == len(bank.candidates)
    counts = Counter(x.strategy for x in bank.candidates if not x.is_anchor)
    assert max(counts.values()) <= 20
    assert sum(x.is_anchor for x in bank.candidates) == 4
    assert bank.bank_sha256 == c.pool_sha256(bank.candidates)
    for candidate in bank.candidates:
        c.Candidate.model_validate(candidate.model_dump())
    for candidate in pinned().candidates:
        assert bayes.coverage_tags(candidate.prices_cents, candidate.discount_percent) == (
            candidate.coverage_tags)
        assert bayes.predictions(c.HypothesisId, candidate.prices_cents,
                                 candidate.discount_percent) == reference.predictions(
                                     c.HypothesisId, candidate.prices_cents,
                                     candidate.discount_percent)
    changed = plan()
    changed = changed.model_copy(update={'hypotheses': [h.model_copy(update={
        'constraints': h.constraints.model_copy(update={'discount_percent': 37})})
        for h in changed.hypotheses]})
    new_bank = bayes.generate(changed, 17)
    assert bank.pool_for(c.Strategy.CONSTRAINT, []) != new_bank.pool_for(c.Strategy.CONSTRAINT, [])


def test_selection_validation():
    belief = bayes.initialize(plan()).belief
    candidate = pinned().candidates[0]
    for pool in ([], [candidate, candidate], [candidate] * 65,
                 [candidate.model_copy(update={'generator_version': 'old'})]):
        with pytest.raises(ValueError):
            bayes.select(belief, pool, policy())
    for field in ('likelihood_policy', 'selection_policy'):
        with pytest.raises(ValueError):
            bayes.select(belief, [candidate], policy().model_copy(update={field: 'old'}))
    for field in ('likelihood_policy', 'prior_policy'):
        with pytest.raises(ValueError):
            bayes.select(belief.model_copy(update={field: 'old'}), [candidate], policy())
    with pytest.raises(ValueError):
        bayes.select(belief, [candidate], policy(0))


def test_ties_and_guard():
    belief = bayes.initialize(plan()).belief
    a = bayes.make_candidate([2], 0, c.Strategy.BOUNDARY)
    b = bayes.make_candidate([4], 0, c.Strategy.BOUNDARY)
    for pool in ([a, b], [b, a]):
        assert bayes.select(belief, pool, policy()).candidate.input_sha256 == min(
            a.input_sha256, b.input_sha256)
    multi = bayes.make_candidate([], 0, c.Strategy.BOUNDARY)
    missing = [c.CoverageTag.EMPTY, c.CoverageTag.ZERO_DISCOUNT]
    for budget in (1, 2):
        chosen = bayes.select(belief, [a, multi], policy(budget, missing))
        assert chosen.candidate == multi
        assert chosen.reason is c.SelectionReason.COVERAGE_GUARD
        assert chosen.candidate_pool_sha256 == c.pool_sha256([a, multi])
    assert bayes.select(belief, [a, multi], policy(3, missing)).reason is (
        c.SelectionReason.MAX_EIG)
    assert bayes.select(belief, [a], policy(1, [c.CoverageTag.EMPTY])).reason is (
        c.SelectionReason.MAX_EIG)


def test_update_validation_and_arithmetic():
    belief = bayes.initialize(plan()).belief
    selection = bayes.select(belief, pinned().candidates, policy())
    for bad in ('match', None, 1):
        with pytest.raises(ValueError):
            bayes.update(belief, selection, bad)
    for field in ('prior', 'likelihood_mismatch', 'predictions'):
        with pytest.raises(ValueError):
            bayes.update(belief, selection.model_copy(update={field: {}}), c.BinaryOutcome.MATCH)
    with pytest.raises(ValueError):
        bayes.update(belief.model_copy(update={'updates': c.MAX_PROBES}), selection,
                     c.BinaryOutcome.MATCH)
    hid = c.HypothesisId.H0
    for prior, q in (({hid: float('nan')}, {hid: .2}),
                     ({hid: 0.0}, {hid: .2}), ({hid: 1.0}, {hid: 0.0})):
        with pytest.raises(ArithmeticError):
            bayes.eig(prior, q)


def test_negative_eig_tolerance(monkeypatch):
    hid = c.HypothesisId.H0
    prior, q = {hid: 1.0}, {hid: .5}
    for error, raises in ((1e-13, False), (1e-3, True)):
        values = iter([0.0, error, error])
        monkeypatch.setattr(bayes, 'entropy', lambda _, values=values: next(values))
        if raises:
            with pytest.raises(ArithmeticError):
                bayes.eig(prior, q)
        else:
            assert bayes.eig(prior, q) == 0.0


@pytest.mark.parametrize('case', ['fixed', 'buggy'])
def test_golden_replay(case):
    receipt = json.loads((catalog.DEMO_DIR / 'receipts' / f'{case}.seed17.json').read_text())
    belief = bayes.initialize(plan()).belief
    bank = pinned()
    used = set()
    missing = set(c.REQUIRED_COVERAGE_TAGS)
    for probe in receipt['probes']:
        pool = bank.pool_for(c.Strategy(probe['route']['choice']), used)
        selected = bayes.select(belief, pool, policy(6 - len(used), missing))
        assert selected.candidate.input_sha256 == probe['candidate']['input_sha256']
        assert selected.reason == probe['selection_reason']
        assert selected.eig_bits == probe['eig_bits']
        assert selected.prior == probe['prior']
        used.add(selected.candidate.input_sha256)
        if probe['posterior'] is not None:
            outcome = c.BinaryOutcome.MATCH if probe['evidence_kind'] == 'match' else (
                c.BinaryOutcome.MISMATCH)
            belief = bayes.update(belief, selected, outcome)
            assert belief.probabilities == probe['posterior']
            if outcome is c.BinaryOutcome.MATCH:
                missing -= set(selected.candidate.coverage_tags)
    selected = bayes.select(bayes.initialize(plan()).belief,
                            bank.pool_for(c.Strategy.CONSTRAINT, []), policy())
    assert selected.candidate.as_input() == c.CandidateInput(
        prices_cents=list(c.DEMO_SENTINEL[0]), discount_percent=c.DEMO_SENTINEL[1])


@pytest.mark.parametrize('case', ['fixed', 'buggy'])
def test_offline_loop(case, monkeypatch):
    for name in ('initialize', 'generate', 'select', 'update'):
        monkeypatch.setattr(simulate.reference, name, getattr(bayes, name))
    result = simulate.simulate_case(case)
    if case == 'buggy':
        assert any(p.evidence_kind is c.EvidenceKind.HEAD_MISMATCH for p in result.probes)
    else:
        assert result.receipt.coverage.complete
        assert len(result.probes) <= 6
