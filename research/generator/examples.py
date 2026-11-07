"""Train/validation generator examples from reviewed Track A cases (test split refused)."""

from __future__ import annotations

import difflib
from collections.abc import Iterator
from typing import Final

from proofhunter.contracts import HypothesisMode, PlanConstraints, SourceRef, Strategy
from research.generator.prompt import build_input
from research.schemas.interfaces import CaseStore
from research.schemas.models import (
    CaseLabel,
    GeneratorExample,
    LabelKind,
    Split,
    TargetHypothesis,
)

#: Defect family keyword -> production mode (anything else is ``other``).
_FAMILY_MODES: Final = (
    (("per_line", "per_item", "line_round"), HypothesisMode.PER_ITEM_ROUNDING),
    (("half_even", "banker", "round_even"), HypothesisMode.HALF_EVEN_ROUNDING),
)
_MODE_TEXT: Final = {
    HypothesisMode.PER_ITEM_ROUNDING: (
        "Head rounds each discounted line before summing instead of rounding once.",
        Strategy.CONSTRAINT,
        "Two or more items whose discounted line totals each leave a fractional cent."),
    HypothesisMode.HALF_EVEN_ROUNDING: (
        "Head rounds exact half-cent ties to even instead of half up.",
        Strategy.BOUNDARY,
        "A discounted subtotal ending in exactly half a cent with an even lower cent."),
    HypothesisMode.OTHER: (
        "Head deviates from the contract formula in an unmodeled way.",
        Strategy.SEEDED_FUZZ,
        "Inputs exercising the changed lines of the head file."),
}


class SplitRefused(PermissionError):
    pass


def family_mode(family: str | None) -> HypothesisMode:
    for keys, mode in _FAMILY_MODES:
        if family and any(k in family for k in keys):
            return mode
    return HypothesisMode.OTHER


def changed_head_lines(base: str, head: str, limit: int = 4) -> list[int]:
    lines: list[int] = []
    matcher = difflib.SequenceMatcher(a=base.splitlines(), b=head.splitlines(), autojunk=False)
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            lines.extend(range(j1 + 1, j2 + 1))
    return lines[:limit]


def target_for(label: CaseLabel, base: str, head: str) -> list[TargetHypothesis]:
    refs = [SourceRef(path="checkout.py", line=n) for n in changed_head_lines(base, head)]
    if label.kind is LabelKind.CLEAN or label.witness is None:
        return [TargetHypothesis(
            mode=HypothesisMode.OTHER,
            rationale="No modeled defect is evident; only unmodeled deviations remain.",
            source_refs=refs, suggested_strategy=Strategy.SEEDED_FUZZ,
            constraints=PlanConstraints(min_items=None, max_items=None, discount_percent=None,
                                        require_fractional_cent=None),
            triggering_conditions="None identified from the change.",
            grounding_witness_sha256=None)]
    mode = family_mode(label.defect_family)
    rationale, strategy, trigger = _MODE_TEXT[mode]
    witness = label.witness.input
    fractional = bool(sum(witness.prices_cents) * (100 - witness.discount_percent) % 100)
    if mode is HypothesisMode.PER_ITEM_ROUNDING:
        constraints = PlanConstraints(min_items=2, max_items=None, discount_percent=None,
                                      require_fractional_cent=True)
    elif mode is HypothesisMode.HALF_EVEN_ROUNDING:
        constraints = PlanConstraints(min_items=None, max_items=None, discount_percent=None,
                                      require_fractional_cent=True)
    else:
        constraints = PlanConstraints(min_items=None, max_items=None, discount_percent=None,
                                      require_fractional_cent=fractional or None)
    return [TargetHypothesis(
        mode=mode, rationale=rationale, source_refs=refs, suggested_strategy=strategy,
        constraints=constraints, triggering_conditions=trigger,
        grounding_witness_sha256=label.witness.input_sha256)]


def build_examples(store: CaseStore, split: Split) -> Iterator[GeneratorExample]:
    """Label-derived targets; inputs never contain template params, fix patches or labels."""
    if split not in (Split.TRAIN, Split.VALIDATION):
        raise SplitRefused(f"split {split!r} is not readable by the generator lane")
    labels = {label.case_id: label for label in store.labels(split.value)}
    for case in store.manifest().cases:
        if case.split is not split:
            continue
        label = labels.get(case.case_id)
        if label is None:
            raise ValueError(f"missing label for {case.case_id}")
        sources = store.sources(case.case_id)
        prompt = build_input(sources)
        yield GeneratorExample(
            schema_version="research-generator-example-v1",
            example_id=f"gen-{case.case_id}", case_id=case.case_id, split=split,
            input=prompt,
            target=target_for(label, sources.base_source.decode(), sources.head_source.decode()),
            teacher=None, contains_template_params=False, contains_fix_patch=False)
