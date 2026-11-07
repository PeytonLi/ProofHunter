"""Tiny smoke examples from the reviewed production demo fixtures (read as bytes, never run).

Smoke-only: these examples exercise the training/inference plumbing and are never used for
reported results or checkpoint selection of the real adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from proofhunter.contracts import HypothesisMode, PlanConstraints, Strategy, input_sha256
from research.generator.examples import changed_head_lines
from research.generator.prompt import build_input
from research.schemas.interfaces import CaseSources
from research.schemas.models import CaseRecord, GeneratorExample, Split, TargetHypothesis

REVISIONS: Final = Path(__file__).resolve().parents[2] / "fixtures/demo/revisions"
_SAMPLE_CASE: Final = (Path(__file__).resolve().parents[1] / "schemas/samples/CaseRecord.json")
#: (base, head, split, mode or None for clean, witness prices, witness discount)
PAIRS: Final = (
    ("base", "buggy", Split.TRAIN, HypothesisMode.PER_ITEM_ROUNDING, [15, 15, 15], 10),
    ("base", "half_even", Split.TRAIN, HypothesisMode.HALF_EVEN_ROUNDING, [5], 10),
    ("base", "fixed", Split.TRAIN, None, None, None),
    ("fixed", "buggy", Split.TRAIN, HypothesisMode.PER_ITEM_ROUNDING, [15, 15, 15], 10),
    ("fixed", "half_even", Split.VALIDATION, HypothesisMode.HALF_EVEN_ROUNDING, [5], 10),
    ("fixed", "base", Split.VALIDATION, None, None, None),
)
_TEXT: Final = {
    HypothesisMode.PER_ITEM_ROUNDING: (
        "Head discounts and rounds each line separately instead of once.", Strategy.CONSTRAINT,
        PlanConstraints(min_items=2, max_items=None, discount_percent=None,
                        require_fractional_cent=True),
        "Two or more items whose discounted lines each leave a fractional cent."),
    HypothesisMode.HALF_EVEN_ROUNDING: (
        "Head rounds exact half-cent ties to the even cent.", Strategy.BOUNDARY,
        PlanConstraints(min_items=None, max_items=None, discount_percent=None,
                        require_fractional_cent=True),
        "A discounted subtotal ending in exactly half a cent with an even lower cent."),
}


def _case() -> CaseRecord:
    return CaseRecord.model_validate_json(_SAMPLE_CASE.read_text())


def smoke_examples() -> tuple[list[GeneratorExample], list[GeneratorExample]]:
    train: list[GeneratorExample] = []
    val: list[GeneratorExample] = []
    case = _case()
    for base_name, head_name, split, mode, prices, discount in PAIRS:
        base = (REVISIONS / base_name / "checkout.py").read_bytes()
        head = (REVISIONS / head_name / "checkout.py").read_bytes()
        prompt = build_input(CaseSources(case=case, base_source=base, head_source=head))
        refs = [{"path": "checkout.py", "line": n}
                for n in changed_head_lines(base.decode(), head.decode())]
        if mode is None:
            target = TargetHypothesis.model_validate({
                "mode": HypothesisMode.OTHER,
                "rationale": "No modeled defect is evident; only unmodeled deviations remain.",
                "source_refs": refs, "suggested_strategy": Strategy.SEEDED_FUZZ,
                "constraints": PlanConstraints(min_items=None, max_items=None,
                                               discount_percent=None,
                                               require_fractional_cent=None),
                "triggering_conditions": "None identified from the change.",
                "grounding_witness_sha256": None})
        else:
            rationale, strategy, constraints, trigger = _TEXT[mode]
            target = TargetHypothesis.model_validate({
                "mode": mode, "rationale": rationale, "source_refs": refs,
                "suggested_strategy": strategy, "constraints": constraints,
                "triggering_conditions": trigger,
                "grounding_witness_sha256": input_sha256(
                    {"prices_cents": prices, "discount_percent": discount})})
        ex = GeneratorExample(
            schema_version="research-generator-example-v1",
            example_id=f"smoke-{base_name}-{head_name}", case_id=f"smoke-{base_name}-{head_name}",
            split=Split.TRAIN if split is Split.TRAIN else Split.VALIDATION,
            input=prompt, target=[target], teacher=None,
            contains_template_params=False, contains_fix_patch=False)
        (train if split is Split.TRAIN else val).append(ex)
    return train, val
