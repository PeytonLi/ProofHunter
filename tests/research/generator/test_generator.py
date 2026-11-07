"""Generator lane: prompt parity, parsing, examples, scoring, ledger (offline, no GPU)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from proofhunter.contracts import HypothesisMode, builtin_hypothesis_payload
from research.generator import pins
from research.generator.completion import completion_text
from research.generator.examples import SplitRefused, build_examples, changed_head_lines
from research.generator.ledger import JsonlLedger, gpu_entry
from research.generator.model import QwenGenerator, load_generator
from research.generator.prompt import (
    BEHAVIOR_SPEC_SHA256,
    PlanParseError,
    build_input,
    parse_plan,
    prompt_sha256,
    render_prompt,
)
from research.generator.scoring import score_generations
from research.schemas.interfaces import (
    CaseSources,
    CaseStore,
    GeneratorTrainer,
    HypothesisGenerator,
)
from research.schemas.models import (
    CaseLabel,
    CaseManifest,
    CaseRecord,
    CostStatus,
    GeneratorExample,
    Split,
)
from research.training.qlora import QloraTrainer, prompt_rows, sft_rows
from research.training.runpod import BudgetRefused, pod_session

ROOT = Path(__file__).resolve().parents[3]
SAMPLES = ROOT / "research/schemas/samples"
BASE = (b"def total_cents(prices_cents, discount_percent):\n"
        b"    subtotal = sum(prices_cents)\n"
        b"    return (subtotal * (100 - discount_percent) + 50) // 100\n")
HEAD = (b"def total_cents(prices_cents, discount_percent):\n"
        b"    lines = [(p * (100 - discount_percent) + 50) // 100 for p in prices_cents]\n"
        b"    return sum(lines)\n")


def sample(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SAMPLES / f"{name}.json").read_text())
    return data


class FakeStore:
    def __init__(self, split: Split) -> None:
        rec = sample("CaseRecord")
        rec["split"] = split.value
        self.case = CaseRecord.model_validate_json(json.dumps(rec))
        self.label = CaseLabel.model_validate_json(json.dumps(sample("CaseLabel")))
        self.unsealed: list[str] = []

    def manifest(self) -> CaseManifest:
        manifest = CaseManifest.model_validate_json(json.dumps(sample("CaseManifest")))
        return manifest.model_copy(update={"cases": [self.case]})

    def sources(self, case_id: str) -> CaseSources:
        assert case_id == self.case.case_id
        return CaseSources(case=self.case, base_source=BASE, head_source=HEAD)

    def labels(self, split: str, *, unseal: str | None = None) -> Sequence[CaseLabel]:
        if split == "test":
            raise PermissionError("sealed")
        self.unsealed.append(split)
        return [self.label]


def example(split: Split = Split.VALIDATION) -> GeneratorExample:
    return next(build_examples(FakeStore(split), split))


def test_build_input_is_label_free_and_pinned() -> None:
    store = FakeStore(Split.TRAIN)
    prompt = build_input(store.sources(store.case.case_id))
    assert prompt.behavior_spec_sha256 == BEHAVIOR_SPEC_SHA256
    assert [f.side for f in prompt.context_files] == ["base", "head"]
    assert prompt.context_files[1].sha256 == hashlib.sha256(HEAD).hexdigest()
    text = render_prompt(prompt)
    assert text.startswith("<|im_start|>system\n")
    assert text.endswith("<|im_start|>assistant\n")
    for leak in ("per_line", "discount_mode", "round_at", "witness", "326b2a87", "expected"):
        assert leak not in text


def test_behavior_spec_matches_production_text() -> None:
    from proofhunter.github import source

    assert hashlib.sha256(source._CONTRACT.encode()).hexdigest() == BEHAVIOR_SPEC_SHA256


def test_render_is_deterministic_and_same_for_both_kinds() -> None:
    ex = example()
    assert render_prompt(ex.input) == render_prompt(ex.input.model_copy(deep=True))
    assert prompt_sha256(ex.input) == hashlib.sha256(render_prompt(ex.input).encode()).hexdigest()


def test_examples_targets_and_test_refusal() -> None:
    ex = example(Split.TRAIN)
    assert ex.split is Split.TRAIN
    assert not ex.contains_template_params
    assert [t.mode for t in ex.target] == [HypothesisMode.PER_ITEM_ROUNDING]
    assert ex.target[0].source_refs
    assert ex.target[0].source_refs[0].line == 2
    assert ex.target[0].grounding_witness_sha256 == sample("CaseLabel")["witness"]["input_sha256"]
    GeneratorExample.model_validate_json(ex.model_dump_json())
    with pytest.raises(SplitRefused):
        next(build_examples(FakeStore(Split.TEST), Split.TEST))
    assert changed_head_lines("a\nb\n", "a\nc\nd\n") == [2, 3]


def test_completion_roundtrips_through_parse_plan() -> None:
    ex = example()
    plan = parse_plan(completion_text(ex))
    assert [p.mode for p in plan.hypotheses] == [HypothesisMode.PER_ITEM_ROUNDING]
    fenced = "```json\n" + completion_text(ex) + "\n```"
    assert parse_plan(fenced) == plan


@pytest.mark.parametrize("bad", ["", "not json", '{"hypotheses": []}', '{"hypotheses": 1}',
                                 '{"hypotheses": [{"mode": "made_up"}]}', "x" * 20000])
def test_parse_plan_rejects(bad: str) -> None:
    with pytest.raises(PlanParseError):
        parse_plan(bad)


def test_parse_plan_dedupes_modes_first_wins() -> None:
    ex = example()
    one = json.loads(completion_text(ex))["hypotheses"][0]
    two = dict(one, rationale="second")
    plan = parse_plan(json.dumps({"hypotheses": [one, two]}))
    assert len(plan.hypotheses) == 1
    assert plan.hypotheses[0].rationale == one["rationale"]


def test_generator_replay_and_fallback() -> None:
    ex = example()
    good = QwenGenerator("g", {prompt_sha256(ex.input): completion_text(ex)})
    assert isinstance(good, HypothesisGenerator)
    assert good.propose(ex.input).hypotheses[0].mode is HypothesisMode.PER_ITEM_ROUNDING
    ungrounded = json.loads(completion_text(ex))
    ungrounded["hypotheses"][0]["source_refs"] = [{"path": "checkout.py", "line": 99}]
    bad = QwenGenerator("g", {prompt_sha256(ex.input): json.dumps(ungrounded)})
    assert bad.propose(ex.input) == builtin_hypothesis_payload()
    assert QwenGenerator("g", {}).propose(ex.input) == builtin_hypothesis_payload()
    with pytest.raises(ValueError, match="revision"):
        load_generator("qwen_untuned", adapter=None, revision="0" * 40)
    with pytest.raises(ValueError, match="adapter"):
        load_generator("qwen_qlora", adapter=None, revision=pins.BASE_REVISION)


def test_scoring_validation_only() -> None:
    ex = example()
    perfect = score_generations([ex], {prompt_sha256(ex.input): completion_text(ex)})
    assert perfect.valid_rate == 1.0
    assert perfect.mode_recall == 1.0
    assert perfect.ref_hit_rate == 1.0
    assert score_generations([ex], {}).score == 0.0
    with pytest.raises(PermissionError):
        score_generations([example(Split.TRAIN)], {})


def test_sft_rows_and_prompt_rows() -> None:
    ex = example()
    rows = sft_rows([ex])
    assert rows[0]["prompt"] == render_prompt(ex.input)
    assert rows[0]["completion"] == completion_text(ex)
    assert prompt_rows([ex])[0]["prompt_sha256"] == prompt_sha256(ex.input)
    assert isinstance(QloraTrainer(run_id="t"), GeneratorTrainer)
    assert isinstance(FakeStore(Split.TRAIN), CaseStore)


def test_trainer_refuses_wrong_splits(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        QloraTrainer(run_id="t").train([example(Split.VALIDATION)], [example()], tmp_path)


def test_ledger_roundtrip_and_paid_rules(tmp_path: Path) -> None:
    ledger = JsonlLedger(tmp_path / "ledger.jsonl")
    entry = gpu_entry(entry_id="r1-pod", provider="runpod", resource_id="pod1", run_id="r1",
                      hours=0.5, unit_price=0.89, estimated_job_usd=2.67,
                      pricing_reference="https://www.runpod.io/pricing", phase="training",
                      wall_ms=1800000.0, teardown_verified=True)
    ledger.append(entry)
    assert ledger.entries() == [entry]
    assert entry.cost_usd == 0.445
    assert entry.cost_status is CostStatus.ESTIMATED
    with pytest.raises(ValueError, match="duplicate"):
        ledger.append(entry)


def test_pod_session_refuses_large_jobs(tmp_path: Path) -> None:
    session = pod_session(run_id="x", gpu="rtx4090", max_hours=60,
                          ledger=JsonlLedger(tmp_path / "l"))
    with pytest.raises(BudgetRefused):
        session.__enter__()


class FakeRunpod:
    def __init__(self) -> None:
        self.pods: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []

    def create_pod(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created.append(body)
        pod: dict[str, Any] = {"id": "pod-abc", "costPerHr": 0.89, "publicIp": "1.2.3.4",
                               "portMappings": {"22": 2222}}
        self.pods["pod-abc"] = pod
        return pod

    def get_pod(self, pod_id: str) -> dict[str, Any] | None:
        return self.pods.get(pod_id)

    def list_pods(self) -> list[dict[str, Any]]:
        return list(self.pods.values())

    def delete_pod(self, pod_id: str) -> None:
        self.pods.pop(pod_id, None)


def test_pod_session_always_tears_down_and_records(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import research.training.runpod as rp

    monkeypatch.setattr(rp, "SSH_DIR", tmp_path / "ssh")
    fake = FakeRunpod()
    ledger = JsonlLedger(tmp_path / "ledger.jsonl")
    seen: list[str] = []

    def crash() -> None:
        with pod_session(run_id="r2", gpu="rtx4090", max_hours=1, ledger=ledger,
                         client=fake) as pod:  # type: ignore[arg-type]
            seen.append(pod.pod_id)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        crash()
    assert seen == ["pod-abc"]
    assert fake.pods == {}
    assert fake.created[0]["imageName"] == pins.POD_IMAGE
    assert "HF_TOKEN" not in fake.created[0]["env"]
    (entry,) = ledger.entries()
    assert entry.teardown_verified is True
    assert entry.resource_id == "pod-abc"
    assert entry.paid
