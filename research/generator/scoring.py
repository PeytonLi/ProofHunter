"""Validation-only generator score used for checkpoint selection (never on test)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from proofhunter.contracts import HypothesisMode
from research.generator.prompt import PlanParseError, check_grounded, parse_plan, prompt_sha256
from research.schemas.models import GeneratorExample, Split


@dataclass(frozen=True)
class GeneratorScore:
    n: int
    valid_rate: float
    mode_recall: float
    clean_precision: float
    ref_hit_rate: float
    score: float


def _rate(a: float, b: int) -> float:
    return a / b if b else 0.0


def score_generations(examples: Iterable[GeneratorExample],
                      generations: Mapping[str, str]) -> GeneratorScore:
    n = valid = ref_hit = clean_n = clean_ok = defect_n = 0
    recall_n = 0.0
    for ex in examples:
        if ex.split is not Split.VALIDATION:
            raise PermissionError("checkpoint selection uses validation examples only")
        n += 1
        text = generations.get(prompt_sha256(ex.input))
        try:
            if text is None:
                raise PlanParseError("missing")
            plan = parse_plan(text)
            check_grounded(plan, ex.input)
        except PlanParseError:
            plan = None
        want = {t.mode for t in ex.target if t.mode is not HypothesisMode.OTHER}
        got = set() if plan is None else {
            p.mode for p in plan.hypotheses if p.mode is not HypothesisMode.OTHER}
        valid += plan is not None
        if want:
            defect_n += 1
            recall_n += len(want & got) / len(want)
            lines = {r.line for t in ex.target for r in t.source_refs}
            refs = set() if plan is None else {r.line for p in plan.hypotheses
                                               for r in p.source_refs}
            near = any(abs(a - b) <= 2 for a in lines for b in refs)
            ref_hit += near or not lines
        else:
            clean_n += 1
            clean_ok += plan is not None and not got
    recall, precision = _rate(recall_n, defect_n), _rate(clean_ok, clean_n)
    hits = _rate(ref_hit, defect_n)
    valid_rate = _rate(valid, n)
    score = 0.5 * recall + 0.2 * precision + 0.15 * hits + 0.15 * valid_rate
    return GeneratorScore(n, valid_rate, recall, precision, hits, round(score, 6))
