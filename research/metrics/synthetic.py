"""Synthetic labels, raw results and ledger entries that follow the frozen schemas.

For tests and report demos only. Rows are generated, never executed; nothing here runs code.
Run: ``python -m research.metrics.synthetic --out DIR``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofhunter.contracts import input_sha256
from research.metrics.results import write_raw_results
from research.schemas.models import (
    CaseLabel,
    CostStatus,
    LabelFile,
    LabelKind,
    LedgerEntry,
    MeasurementMode,
    ProtocolV1,
    RawResultRow,
    Split,
)

SPEC = Path(__file__).resolve().parents[1] / "spec" / "protocol-v1.json"
FAKE_SHA = "0" * 40


@dataclass(frozen=True)
class ArmProfile:
    detect: float  # probability a defect episode yields a confirmed witness
    mean_step: float  # geometric mean pair index of the witness
    inconclusive: float = 0.05
    false_positive: float = 0.0
    usd_per_pair: float | None = 0.0
    ms_per_pair: float = 5.0
    generation_ms: float | None = None


DEFAULT_PROFILES: Mapping[str, ArmProfile] = {
    "fixed_order": ArmProfile(0.55, 3.5),
    "uniform_random": ArmProfile(0.5, 3.8),
    "builtin_eig": ArmProfile(0.75, 2.2),
    "builtin_ranker": ArmProfile(0.78, 2.0, ms_per_pair=6.0),
    "frontier_agent": ArmProfile(
        0.8,
        2.5,
        inconclusive=0.08,
        false_positive=0.02,
        usd_per_pair=0.004,
        ms_per_pair=900.0,
        generation_ms=2500.0,
    ),
}


def load_protocol(path: Path = SPEC) -> ProtocolV1:
    return ProtocolV1.model_validate_json(path.read_text(encoding="utf-8"))


def _hex(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def make_labels(
    n_regression: int,
    n_clean: int,
    n_existing: int = 0,
    *,
    families: Sequence[str] = (
        "per_line",
        "half_even",
        "ignored_discount",
        "item_count",
        "aggregate_order",
    ),
) -> list[CaseLabel]:
    labels: list[CaseLabel] = []
    witness_input = {"prices_cents": [15, 15, 15], "discount_percent": 10}
    for i in range(n_regression + n_existing):
        existing = i >= n_regression
        data: dict[str, Any] = {
            "case_id": f"syn-{'ex' if existing else 'rg'}-{i:04d}",
            "kind": "existing_defect" if existing else "introduced_regression",
            "defect_family": families[i % len(families)],
            "expected_verdict": (
                "confirmed_existing_defect" if existing else "confirmed_regression"
            ),
            "witness": {
                "input": witness_input,
                "input_sha256": input_sha256(witness_input),
                "expected_cents": 41,
                "base_cents": 42 if existing else 41,
                "head_cents": 42,
                "reproduced_by": "reviewed_template_execution",
            },
            "review_rationale": None,
        }
        labels.append(CaseLabel.model_validate_json(json.dumps(data)))
    for i in range(n_clean):
        data = {
            "case_id": f"syn-cl-{i:04d}",
            "kind": "clean",
            "defect_family": None,
            "expected_verdict": "no_defect_found",
            "witness": None,
            "review_rationale": "Synthetic clean refactor control.",
        }
        labels.append(CaseLabel.model_validate_json(json.dumps(data)))
    return labels


def label_file(labels: Sequence[CaseLabel], split: Split) -> LabelFile:
    return LabelFile.model_validate_json(
        json.dumps(
            {
                "schema_version": "research-labels-v1",
                "split": split.value,
                "sealed": split is Split.TEST,
                "manifest_cases_sha256": _hex("manifest", split.value),
                "labels": [lab.model_dump(mode="json") for lab in labels],
            }
        )
    )


def _probe(
    case: str, seed: int, step: int, outcome: str, confirmation: str = "not_run", retries: int = 0
) -> dict[str, Any]:
    return {
        "step": step,
        "candidate_input_sha256": _hex(case, seed, step),
        "outcome": outcome,
        "confirmation": confirmation,
        "retries": retries,
    }


def make_row(
    label: CaseLabel,
    arm: str,
    seed: int,
    mode: MeasurementMode,
    profile: ArmProfile,
    rng: random.Random,
    *,
    repetition: int = 0,
    split: Split = Split.VALIDATION,
    max_pairs: int = 6,
) -> RawResultRow:
    case = label.case_id
    step = min(max_pairs, 1 + int(rng.expovariate(1 / max(profile.mean_step - 1, 1e-6))))
    probes: list[dict[str, Any]]
    first: int | None = None
    if label.kind is LabelKind.CLEAN and rng.random() < profile.false_positive:
        probes = [_probe(case, seed, s, "match") for s in range(1, step)]
        probes.append(_probe(case, seed, step, "mismatch", "reproduced"))
        verdict, reason, first = "confirmed_regression", "confirmed_regression", step
    elif rng.random() < profile.inconclusive:
        probes = [_probe(case, seed, s, "match") for s in range(1, step)]
        probes.append(_probe(case, seed, step, "unusable", retries=1))
        verdict, reason = rng.choice(
            [
                ("inconclusive", "unusable_probe_timeout"),
                ("inconclusive", "missing_coverage"),
                ("failed", "infrastructure_failure"),
            ]
        )
    elif label.kind is LabelKind.CLEAN or rng.random() >= profile.detect:
        probes = [_probe(case, seed, s, "match") for s in range(1, max_pairs + 1)]
        verdict, reason = ("no_defect_found", "no_defect_found")
    elif label.kind is LabelKind.EXISTING_DEFECT:
        probes = [_probe(case, seed, s, "match") for s in range(1, step)]
        probes.append(_probe(case, seed, step, "base_mismatch"))
        verdict, reason = "confirmed_existing_defect", "confirmed_existing_defect"
    else:
        probes = [_probe(case, seed, s, "match") for s in range(1, step)]
        probes.append(_probe(case, seed, step, "mismatch", "reproduced"))
        verdict, reason, first = "confirmed_regression", "confirmed_regression", step
    n = len(probes)
    confirm = int(any(p["confirmation"] != "not_run" for p in probes))
    execution = n * profile.ms_per_pair * rng.uniform(0.8, 1.25)
    confirmation_ms = profile.ms_per_pair if confirm else 0.0
    generation = profile.generation_ms
    wall = execution + confirmation_ms + (generation or 0.0) + 0.5
    if mode is MeasurementMode.OFFLINE or profile.usd_per_pair == 0.0:
        cost, status = 0.0, CostStatus.MEASURED
    elif profile.usd_per_pair is None:
        cost, status = None, CostStatus.UNKNOWN
    else:
        cost = (n + confirm) * profile.usd_per_pair
        status = CostStatus.ESTIMATED if mode is MeasurementMode.RECORDED else CostStatus.MEASURED
    data = {
        "schema_version": "research-raw-result-v1",
        "protocol_version": "research-protocol-v1",
        "run_id": f"{mode.value}-{arm}-{case}-{seed}-{repetition}",
        "implementation_sha": FAKE_SHA,
        "track": "track_a",
        "case_id": case,
        "split": split.value,
        "arm_id": arm,
        "seed": seed,
        "repetition": repetition,
        "measurement_mode": mode.value,
        "snap_routing": "disabled",
        "pool_sha256": _hex("pool", case, seed),
        "models": {
            "generator_model_id": None,
            "generator_revision": None,
            "adapter_sha256": None,
            "ranker_checkpoint_sha256": None,
            "frontier_model_id": "gpt-6-sol" if arm.startswith("frontier") else None,
            "prompt_sha256": None,
        },
        "probes": probes,
        "invocations": {
            "base": n,
            "head": n,
            "confirm": confirm,
            "failed": sum(p["outcome"] == "unusable" for p in probes),
        },
        "verdict": verdict,
        "reason_code": reason,
        "pairs_to_first_confirmed_witness": first,
        "censored": first is None,
        "timings": {
            "wall_ms": wall,
            "queue_ms": 0.2,
            "generation_ms": generation,
            "feature_ms": 0.1 if "ranker" in arm else None,
            "ranking_ms": 0.2,
            "sandbox_startup_ms": None,
            "execution_ms": execution,
            "confirmation_ms": confirmation_ms,
            "cleanup_ms": None,
            "cold_start": repetition == 0 and seed == 0,
        },
        "tokens": {
            "input_tokens": 1200 * n if generation else None,
            "output_tokens": 80 * n if generation else None,
            "reasoning_tokens": None,
        },
        "cost_usd": cost,
        "cost_status": status.value,
    }
    return RawResultRow.model_validate_json(json.dumps(data))


def make_rows(
    labels: Sequence[CaseLabel],
    protocol: ProtocolV1,
    *,
    mode: MeasurementMode,
    profiles: Mapping[str, ArmProfile] = DEFAULT_PROFILES,
    split: Split = Split.VALIDATION,
    seed: int = 7,
) -> list[RawResultRow]:
    rng = random.Random(seed)  # noqa: S311 - synthetic data
    return [
        make_row(label, arm, s, mode, profile, rng, split=split, max_pairs=protocol.max_pairs)
        for arm, profile in profiles.items()
        for label in labels
        for s in protocol.replicate_seeds
    ]


def make_ledger_entries(mode: MeasurementMode) -> list[LedgerEntry]:
    base = {
        "schema_version": "research-ledger-v1",
        "lane": "measurement",
        "resource_id": None,
        "run_id": None,
        "case_id": None,
        "unit_price_usd": None,
        "queue_ms": None,
        "measurement_mode": mode.value,
        "coordinator_checkin_ref": None,
        "teardown_verified": None,
        "pricing_reference": None,
        "pricing_date": None,
        "estimated_job_usd": None,
        "recorded_at": "2026-11-07T23:00:00Z",
    }
    paid = mode is not MeasurementMode.OFFLINE
    rows: list[dict[str, Any]] = [
        {
            "entry_id": f"syn-{mode.value}-exec",
            "provider": "local",
            "category": "execution_infra",
            "phase": "evaluation",
            "quantity": 1.0,
            "unit": "episode_batch",
            "cost_usd": 0.0,
            "cost_status": "measured",
            "wall_ms": 1200.0,
            "paid": False,
        },
        {
            "entry_id": f"syn-{mode.value}-storage",
            "provider": "local",
            "category": "storage_idle",
            "phase": "evaluation",
            "quantity": 0.1,
            "unit": "gb_month",
            "cost_usd": None,
            "cost_status": "unknown",
            "wall_ms": None,
            "paid": False,
        },
    ]
    if paid:
        rows.append(
            {
                "entry_id": f"syn-{mode.value}-gpu",
                "provider": "runpod",
                "category": "training_gpu",
                "phase": "training",
                "quantity": 1.5,
                "unit": "gpu_hour",
                "unit_price_usd": 0.44,
                "cost_usd": 0.66,
                "cost_status": "estimated",
                "wall_ms": 5.4e6,
                "paid": True,
                "pricing_reference": "synthetic",
                "pricing_date": "2026-11-07",
                "estimated_job_usd": 1.0,
                "resource_id": f"pod-{mode.value}",
                "teardown_verified": True,
            }
        )
        rows.append(
            {
                "entry_id": f"syn-{mode.value}-llm",
                "provider": "openai",
                "category": "model_inference",
                "phase": "evaluation",
                "quantity": 1e5,
                "unit": "token",
                "cost_usd": 0.31,
                "cost_status": "estimated",
                "wall_ms": 9e4,
                "paid": True,
                "pricing_reference": "synthetic",
                "pricing_date": "2026-11-07",
                "estimated_job_usd": 0.5,
            }
        )
    return [LedgerEntry.model_validate_json(json.dumps(base | r)) for r in rows]


def write_demo(
    out: Path, *, regressions: int = 40, clean: int = 15, existing: int = 5, seed: int = 7
) -> dict[str, Path]:
    protocol = load_protocol()
    labels = make_labels(regressions, clean, existing)
    out.mkdir(parents=True, exist_ok=True)
    rows = [
        r
        for mode in MeasurementMode
        for r in make_rows(labels, protocol, mode=mode, seed=seed + len(mode.value))
    ]
    results, _ = write_raw_results(out / "results.jsonl", rows)
    labels_path = out / "labels-validation.json"
    labels_path.write_text(label_file(labels, Split.VALIDATION).model_dump_json(indent=2))
    ledger = out / "ledger.jsonl"
    ledger.write_text(
        "".join(
            e.model_dump_json() + "\n"
            for mode in MeasurementMode
            for e in make_ledger_entries(mode)
        )
    )
    degraded = out / "degraded.jsonl"
    reasons = ("timeout", "malformed_output", "empty_ranking")
    degraded.write_text(
        "".join(
            json.dumps(
                {
                    "run_id": r.run_id,
                    "degraded": reasons[int(hashlib.sha256(r.run_id.encode()).hexdigest(), 16) % 3]
                    if int(hashlib.sha256(r.run_id.encode()).hexdigest(), 16) % 5 == 0
                    else None,
                },
                sort_keys=True,
            )
            + "\n"
            for r in rows
            if r.arm_id.startswith("frontier")
        )
    )
    return {"results": results, "labels": labels_path, "ledger": ledger, "degraded": degraded}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write synthetic schema-valid demo inputs.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    for name, path in write_demo(args.out).items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
