"""Supervised completion: the production plan JSON the model must emit for a target."""

from __future__ import annotations

import json

from proofhunter.contracts import HypothesisPlanPayload, HypothesisProposal
from research.schemas.models import GeneratorExample


def target_payload(example: GeneratorExample) -> HypothesisPlanPayload:
    return HypothesisPlanPayload(hypotheses=[
        HypothesisProposal(mode=t.mode, rationale=t.rationale[:300], source_refs=t.source_refs,
                           suggested_strategy=t.suggested_strategy, constraints=t.constraints)
        for t in example.target])


def completion_text(example: GeneratorExample) -> str:
    return json.dumps(target_payload(example).model_dump(mode="json"), sort_keys=True,
                      separators=(",", ":"))
