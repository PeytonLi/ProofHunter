"""R1 feasibility pilot: label reproduction, candidate availability, EIG applicability, arm
episodes, cost/latency per usable case and the projected full-run budget."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Protocol

from proofhunter import bayes, oracle
from proofhunter.contracts import ObservationOutcome
from research.execution.lanes import LaneMissing
from research.execution.pool import shared_pool
from research.execution.runner import ResearchEpisodeRunner
from research.schemas.interfaces import CaseSources, CaseStore, HypothesisGenerator, Selector
from research.schemas.models import (
    ArmSpec,
    CaseLabel,
    LabelKind,
    MeasurementMode,
    ProtocolV1,
    RawResultRow,
    Split,
)


class Factory(Protocol):
    def selector(self, spec: ArmSpec, seed: int) -> Any: ...

    def generator(self, spec: ArmSpec) -> Any: ...


def _value(runner: ResearchEpisodeRunner, side: str, source: bytes, prices: list[int],
           discount: int) -> int | None:
    from proofhunter.contracts import CandidateInput

    result = runner.executor(side, source, CandidateInput(prices_cents=prices,
                                                          discount_percent=discount))
    return result.value if result.outcome is ObservationOutcome.OK else None


def audit_case(runner: ResearchEpisodeRunner, sources: CaseSources, label: CaseLabel | None,
               protocol: ProtocolV1) -> dict[str, Any]:
    """Reviewed-code checks only: witness reproduction and exhaustive pool exposure."""
    review = runner.review(sources)
    out: dict[str, Any] = {"case_id": sources.case.case_id, "source_review": review or "ok",
                           "label_kind": label.kind.value if label else None}
    if review is not None:
        return out
    if label is not None and label.witness is not None:
        w = label.witness
        prices, discount = list(w.input.prices_cents), w.input.discount_percent
        base = _value(runner, "base", sources.base_source, prices, discount)
        head = _value(runner, "head", sources.head_source, prices, discount)
        expected = oracle.rounding.rounding_half_up_once(prices, discount)
        out["witness_reproduced"] = (base, head, expected) == (w.base_cents, w.head_cents,
                                                               w.expected_cents)
    bank = shared_pool(sources.case, protocol)
    exposing, modeled = 0, Counter[str]()
    for c in bank.candidates:
        prices, discount = list(c.prices_cents), c.discount_percent
        expected = oracle.rounding.rounding_half_up_once(prices, discount)
        base = _value(runner, "base", sources.base_source, prices, discount)
        head = _value(runner, "head", sources.head_source, prices, discount)
        if head is not None and head != expected:
            exposing += 1
            for name, fn in (("per_item_rounding", bayes.predict_per_item),
                             ("half_even_rounding", bayes.predict_half_even)):
                if fn(tuple(prices), discount) == head:
                    modeled[name] += 1
        del base
    out.update({"pool_sha256": bank.bank_sha256, "pool_size": len(bank.candidates),
                "pool_exposing_inputs": exposing,
                "eig_modeled_exposures": dict(modeled),
                "eig_applicable": bool(modeled)})
    return out


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def summarize(rows: Sequence[RawResultRow], labels: Mapping[str, CaseLabel],
              protocol: ProtocolV1, skipped: Mapping[str, str]) -> dict[str, Any]:
    per_arm: dict[str, list[RawResultRow]] = defaultdict(list)
    for row in rows:
        per_arm[row.arm_id].append(row)
    arms: dict[str, Any] = {}
    for arm_id, arm_rows in sorted(per_arm.items()):
        usable = [r for r in arm_rows
                  if r.verdict != "failed" and r.reason_code != "source_invalid"]
        invocations = [r.invocations.base + r.invocations.head + r.invocations.confirm
                       for r in arm_rows]
        walls = [r.timings.wall_ms for r in arm_rows]
        startups = [r.timings.sandbox_startup_ms for r in arm_rows
                    if r.timings.sandbox_startup_ms is not None]
        cleanups = [r.timings.cleanup_ms for r in arm_rows if r.timings.cleanup_ms is not None]
        executions = [r.timings.execution_ms for r in arm_rows
                      if r.timings.execution_ms is not None]
        costs = [r.cost_usd for r in arm_rows]
        defect = [r for r in usable if (lab := labels.get(r.case_id)) is not None
                  and lab.kind is not LabelKind.CLEAN]
        hits = {k: sum(1 for r in defect if labels[r.case_id].expected_verdict.value == (
            r.verdict if isinstance(r.verdict, str) else r.verdict.value)
            and len(r.probes) <= k) for k in protocol.budgets_reported}
        clean = [r for r in usable if (lab := labels.get(r.case_id)) is not None
                 and lab.kind is LabelKind.CLEAN]
        arms[arm_id] = {
            "episodes": len(arm_rows), "usable_episodes": len(usable),
            "verdicts": dict(Counter(str(getattr(r.verdict, "value", r.verdict))
                                     for r in arm_rows)),
            "reason_codes": dict(Counter(r.reason_code for r in arm_rows)),
            "defect_episodes": len(defect),
            "descriptive_recall_at": {str(k): (v / len(defect) if defect else None)
                                      for k, v in hits.items()},
            "clean_false_positives": sum(1 for r in clean if str(getattr(
                r.verdict, "value", r.verdict)).startswith("confirmed")),
            "invocations_mean": statistics.fmean(invocations) if invocations else None,
            "invocations_max": max(invocations, default=None),
            "failed_invocations": sum(r.invocations.failed for r in arm_rows),
            "sandbox_startup_ms_mean": statistics.fmean(startups) if startups else None,
            "execution_ms_mean": statistics.fmean(executions) if executions else None,
            "cleanup_ms_mean": statistics.fmean(cleanups) if cleanups else None,
            "wall_ms_p50": _pct(walls, 0.5), "wall_ms_p95": _pct(walls, 0.95),
            "wall_ms_per_usable_case": (sum(walls) / len(usable)) if usable else None,
            "cost_usd_total": (None if any(c is None for c in costs)
                               else float(sum(c or 0.0 for c in costs))),
            "cost_usd_per_usable_case": (None if any(c is None for c in costs) or not usable
                                         else float(sum(c or 0.0 for c in costs)) / len(usable)),
            "cost_status": sorted({r.cost_status.value for r in arm_rows}),
            "measurement_modes": sorted({r.measurement_mode.value for r in arm_rows}),
        }
    return {"arms": arms, "skipped_arms": dict(skipped)}


def project(summary: Mapping[str, Any], protocol: ProtocolV1,
            test_cases: int | None = None) -> dict[str, Any]:
    """Project the frozen test evaluation: test cases x 13 arms x 5 seeds x executions."""
    target = protocol.split_targets["test"]
    cases = test_cases if test_cases is not None else target.defect_cases + target.clean_controls
    source = ("manifest test split (defect+clean)" if test_cases is not None
              else "protocol split_targets test (defect+clean)")
    reps = len(protocol.replicate_seeds)
    n_arms = len(protocol.arms)
    measured = summary["arms"]
    expected_inv = sum(a["invocations_mean"] or 0.0 for a in measured.values())
    mean_inv = expected_inv / len(measured) if measured else None
    per_arm_cost = {k: v["cost_usd_per_usable_case"] for k, v in measured.items()}
    unknown = sorted({a.arm_id for a in protocol.arms} - {k for k, v in per_arm_cost.items()
                                                         if v is not None})
    known_cost = sum((v or 0.0) * cases * reps for v in per_arm_cost.values() if v is not None)
    return {
        "formula": "cases x arms x repetitions x executions_per_episode",
        "cases": cases, "cases_source": source,
        "arms": n_arms, "repetitions": reps,
        "expected_executions_per_episode": mean_inv,
        "expected_executions_total": (cases * n_arms * reps * mean_inv) if mean_inv else None,
        "worst_case_executions_total": cases * n_arms * reps
        * protocol.max_invocations_per_episode,
        "projected_usd_measured_arms": known_cost,
        "projected_usd_by_arm": {k: (v * cases * reps if v is not None else None)
                                 for k, v in sorted(per_arm_cost.items())},
        "arms_with_unknown_cost": unknown,
        "note": "Execution is local reviewed template code in isolated child processes "
                "(0 USD). Model-arm dollars stay unknown until their lanes report measured "
                "per-episode costs.",
    }


def degraded_reason(selector: object, generator: object) -> str | None:
    """The baseline selector's ``degraded`` reason, else a generator fallback, else ``None``."""
    reason = getattr(selector, "degraded", None)
    if reason is not None:
        return str(reason)
    last = getattr(generator, "last", None)
    if last is not None and getattr(last, "mode", None) == "fallback":
        code = getattr(last, "reason_code", None)
        return f"generator_fallback:{getattr(code, 'value', code)}"
    return None


def write_degraded(path: Path, records: Sequence[Mapping[str, str | None]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def degraded_by_arm(rows: Sequence[RawResultRow],
                    records: Sequence[Mapping[str, str | None]]) -> dict[str, dict[str, int]]:
    arm = {r.run_id: r.arm_id for r in rows}
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        if record["degraded"] is not None and record["run_id"] is not None:
            out[arm[record["run_id"]]][record["degraded"]] += 1
    return {k: dict(v) for k, v in sorted(out.items())}


def run(store: CaseStore, protocol: ProtocolV1, runner: ResearchEpisodeRunner,
        factory: Factory, *, arms: Sequence[str], split: Split, max_cases: int,
        seeds: Sequence[int], mode: MeasurementMode, out: Path) -> dict[str, Any]:
    if split is Split.TEST:
        raise ValueError("the pilot never touches the sealed test split")
    start = perf_counter_ns()
    out.mkdir(parents=True, exist_ok=True)
    manifest = store.manifest()
    cases = sorted((c for c in manifest.cases if c.split is split),
                   key=lambda c: hashlib.sha256(c.case_id.encode()).hexdigest())
    cases = cases[:max_cases]
    labels = {lab.case_id: lab for lab in store.labels(split.value)}
    specs = runner.arm_specs()
    unknown_arms = [a for a in arms if a not in specs]
    if unknown_arms:
        raise ValueError(f"arms not in protocol: {unknown_arms}")
    audits = [audit_case(runner, store.sources(c.case_id), labels.get(c.case_id), protocol)
              for c in cases]
    rows: list[RawResultRow] = []
    degraded: list[dict[str, str | None]] = []
    skipped: dict[str, str] = {}
    for arm_id in arms:
        spec = specs[arm_id]
        try:
            generator: HypothesisGenerator | None = factory.generator(spec)
            factory.selector(spec, seeds[0])
        except LaneMissing as error:
            skipped[arm_id] = str(error)
            continue
        for case in cases:
            sources = store.sources(case.case_id)
            for repetition, seed in enumerate(seeds):
                selector: Selector = factory.selector(spec, seed)
                rows.append(runner.run(sources, selector, generator, arm_id=arm_id, seed=seed,
                                       repetition=repetition, mode=mode))
                degraded.append({"run_id": rows[-1].run_id,
                                 "degraded": degraded_reason(selector, generator)})
    with (out / "rows.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(row.model_dump_json() + "\n")
    with (out / "events.jsonl").open("w", encoding="utf-8") as stream:
        for event in runner.events:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
    write_degraded(out / "degraded.jsonl", degraded)
    summary = summarize(rows, labels, protocol, skipped)
    usable_audits = [a for a in audits if a["source_review"] == "ok"]
    report = {
        "schema": "research-pilot-report-v1", "protocol_version": protocol.schema_version,
        "implementation_sha": runner.implementation, "measurement_mode": mode.value,
        "split": split.value, "manifest_version": manifest.manifest_version,
        "manifest_cases_sha256": manifest.cases_sha256, "cases": len(cases),
        "seeds": list(seeds), "arms_requested": list(arms),
        "dataset_checks": {
            "source_review_failures": dict(Counter(a["source_review"] for a in audits
                                                   if a["source_review"] != "ok")),
            "labelled_cases": sum(1 for a in audits if a["label_kind"] is not None),
            "witness_reproduced": sum(1 for a in usable_audits if a.get("witness_reproduced")),
            "witness_not_reproduced": [a["case_id"] for a in usable_audits
                                       if a.get("witness_reproduced") is False],
            "defect_cases_without_exposing_pool_input": [
                a["case_id"] for a in usable_audits
                if a["label_kind"] not in (None, "clean") and not a["pool_exposing_inputs"]],
            "clean_cases_with_exposing_pool_input": [
                a["case_id"] for a in usable_audits
                if a["label_kind"] == "clean" and a["pool_exposing_inputs"]],
            "eig_applicable_defect_cases": sum(1 for a in usable_audits
                                               if a["label_kind"] not in (None, "clean")
                                               and a["eig_applicable"]),
            "defect_cases": sum(1 for a in usable_audits if a["label_kind"] not in (None, "clean")),
        },
        **summary,
        "degraded_by_arm": degraded_by_arm(rows, degraded),
        "projection": project(summary, protocol,
                              sum(c.split is Split.TEST for c in manifest.cases)),
        "rows_sha256": hashlib.sha256((out / "rows.jsonl").read_bytes()).hexdigest(),
        "pilot_wall_ms": (perf_counter_ns() - start) / 1e6,
    }
    (out / "audits.json").write_text(json.dumps(audits, indent=2, sort_keys=True) + "\n")
    (out / "pilot-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out / "pilot-report.md").write_text(render_markdown(report))
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    checks = report["dataset_checks"]
    lines = [
        "# R1 feasibility pilot", "",
        f"Mode: {report['measurement_mode']} (reviewed templates, one isolated local child "
        "process per invocation; not a cloud or sandbox timing). Split: "
        f"{report['split']}. Cases: {report['cases']}. Seeds: {len(report['seeds'])}. "
        f"Implementation: `{report['implementation_sha']}`.", "",
        "## Dataset checks", "",
        f"Defect cases {checks['defect_cases']}, witnesses reproduced "
        f"{checks['witness_reproduced']}, not reproduced {len(checks['witness_not_reproduced'])}, "
        f"defect cases without an exposing pool input "
        f"{len(checks['defect_cases_without_exposing_pool_input'])}, EIG-modeled defect cases "
        f"{checks['eig_applicable_defect_cases']}, source review failures "
        f"{checks['source_review_failures'] or 'none'}.", "",
        "## Arms (descriptive, small sample)", "",
        "| arm | episodes | usable | recall@1 | recall@3 | recall@6 | clean FP | mean invocations "
        "| p50 ms | p95 ms | startup ms | exec ms | cleanup ms | USD per usable case |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def fmt(value: object) -> str:
        return "unknown" if value is None else (f"{value:.3g}" if isinstance(value, float)
                                                else str(value))

    for arm_id, a in report["arms"].items():
        recall = a["descriptive_recall_at"]
        lines.append(
            f"| {arm_id} | {a['episodes']} | {a['usable_episodes']} | {fmt(recall.get('1'))} | "
            f"{fmt(recall.get('3'))} | {fmt(recall.get('6'))} | {a['clean_false_positives']} | "
            f"{fmt(a['invocations_mean'])} | {fmt(a['wall_ms_p50'])} | {fmt(a['wall_ms_p95'])} | "
            f"{fmt(a['sandbox_startup_ms_mean'])} | {fmt(a['execution_ms_mean'])} | "
            f"{fmt(a['cleanup_ms_mean'])} | {fmt(a['cost_usd_per_usable_case'])} |")
    if report["skipped_arms"]:
        lines += ["", "Skipped arms: " + "; ".join(
            f"{k} ({v})" for k, v in sorted(report["skipped_arms"].items()))]
    p = report["projection"]
    lines += ["", "## Projected full run", "",
              f"{p['cases']} cases x {p['arms']} arms x {p['repetitions']} repetitions x "
              f"{fmt(p['expected_executions_per_episode'])} executions per episode = "
              f"{fmt(p['expected_executions_total'])} executions; worst case "
              f"{p['worst_case_executions_total']} (15 per episode). Projected dollars for "
              f"measured arms {fmt(p['projected_usd_measured_arms'])}; unknown for "
              f"{', '.join(p['arms_with_unknown_cost']) or 'none'}. {p['note']}", ""]
    return "\n".join(lines)
