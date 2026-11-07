"""Generator SFT examples for train/validation defect cases, grounded in reproduced witnesses.

Inputs carry only the contract, behavior spec hash, interface, base/head files and their diff;
never template params, family/template ids, labels, fix patches or mutation descriptions.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from proofhunter import contracts as c
from research.data import templates as T
from research.schemas import interfaces as I
from research.schemas import models as M

PROMPT_FORMAT: Final = "gen-prompt-v1"
BEHAVIOR_SPEC: Final = (
    "checkout-rounding@1: total_cents(prices_cents, discount_percent) returns "
    "(sum(prices_cents) * (100 - discount_percent) + 50) // 100 for 0..8 prices in 0..10000 "
    "cents and an integer discount 0..100: discount the subtotal once, round half-up once.")
BEHAVIOR_SPEC_SHA256: Final = hashlib.sha256(BEHAVIOR_SPEC.encode()).hexdigest()
MODE_BY_FAMILY: Final = {"per_line_rounding": c.HypothesisMode.PER_ITEM_ROUNDING,
                         "half_even_rounding": c.HypothesisMode.HALF_EVEN_ROUNDING}


def unified_diff(base: str, head: str) -> str:
    return "".join(difflib.unified_diff(base.splitlines(True), head.splitlines(True),
                                        "a/checkout.py", "b/checkout.py"))


def first_changed_head_line(base: str, head: str) -> int:
    matcher = difflib.SequenceMatcher(a=base.splitlines(), b=head.splitlines())
    for tag, _, _, j1, _ in matcher.get_opcodes():
        if tag != "equal":
            return j1 + 1
    return 1


def example(case: M.CaseRecord, src: I.CaseSources, label: M.CaseLabel) -> M.GeneratorExample:
    assert label.witness is not None and label.defect_family is not None
    base, head = src.base_source.decode(), src.head_source.decode()
    w = label.witness
    prices, d = w.input.prices_cents, w.input.discount_percent
    residue = sum(prices) * (100 - d) % 100
    target = M.TargetHypothesis(
        mode=MODE_BY_FAMILY.get(label.defect_family, c.HypothesisMode.OTHER),
        rationale=f"Head deviates from the contract: {T.FAMILY_BY_ID[label.defect_family].description}.",
        source_refs=[c.SourceRef(path="checkout.py", line=first_changed_head_line(base, head))],
        suggested_strategy=c.Strategy.CONSTRAINT,
        constraints=c.PlanConstraints(min_items=len(prices), max_items=None, discount_percent=d,
                                      require_fractional_cent=residue != 0),
        triggering_conditions=(f"prices_cents={prices}, discount_percent={d}: contract "
                               f"{w.expected_cents} cents, head {w.head_cents} cents (reproduced)"),
        grounding_witness_sha256=w.input_sha256)
    return M.GeneratorExample(
        schema_version="research-generator-example-v1", example_id=f"gen-{case.case_id}",
        case_id=case.case_id, split=case.split,
        input=M.GeneratorInput(
            prompt_format_version=PROMPT_FORMAT, contract_id="checkout-rounding@1",
            behavior_spec_sha256=BEHAVIOR_SPEC_SHA256,
            allowed_test_interface="checkout.total_cents(prices_cents, discount_percent)",
            context_files=[
                M.ContextFile(side="base", path="checkout.py",
                              sha256=case.base_source_sha256, content=base),
                M.ContextFile(side="head", path="checkout.py",
                              sha256=case.head_source_sha256, content=head)],
            unified_diff=unified_diff(base, head)),
        target=[target], teacher=None, contains_template_params=False, contains_fix_patch=False)


def build_examples(store: I.CaseStore, split: M.Split) -> list[M.GeneratorExample]:
    if split not in (M.Split.TRAIN, M.Split.VALIDATION):
        raise ValueError("generator examples cover train and validation only")
    labels = {lab.case_id: lab for lab in store.labels(split.value)}
    return [example(case, store.sources(case.case_id), labels[case.case_id])
            for case in store.manifest().cases
            if case.split is split and labels[case.case_id].witness is not None]


def write(rows: Sequence[M.GeneratorExample], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r.model_dump(mode="json"), sort_keys=True) + "\n"
                            for r in rows))
