"""Markdown benchmark report plus SVG figures from ``research.metrics.scoring.score`` output.

Offline, recorded and live measurements get separate sections, tables and figures; nothing is
pooled across modes. Small samples are labelled descriptive. Unknown costs print as "unknown".
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from research.reporting.plots import bar_chart, line_chart

MODE_ORDER = ("offline", "recorded", "live")
MODE_NOTES = {
    "offline": "Offline measurements: local reviewed-fixture execution. Timings are local fixture "
    "timings, not cloud latency; dollar figures are not provider costs.",
    "recorded": "Recorded measurements: replayed provider transcripts. Costs are estimates from "
    "recorded usage at the stated pricing date, not live charges.",
    "live": "Live measurements: real provider calls and sandbox executions. Only these may be "
    "described as live cloud latency or cost.",
}
TIMING_LABELS = (
    "queue_ms",
    "generation_ms",
    "feature_ms",
    "ranking_ms",
    "sandbox_startup_ms",
    "execution_ms",
    "confirmation_ms",
    "cleanup_ms",
)


def _f(value: object, digits: int = 3) -> str:
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _usd(value: object) -> str:
    return "unknown" if value is None else f"${cast(float, value):.4f}"


def _ci(ci: Mapping[str, Any]) -> str:
    if ci.get("estimate") is None:
        return "NA"
    return f"{ci['estimate']:.3f} [{ci['low']:.3f}, {ci['high']:.3f}]"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(r) + " |" for r in rows),
        "",
    ]


def _write(path: Path, svg: str) -> str:
    path.write_text(svg, encoding="utf-8")
    return path.name


def _figures(prefix: str, out_dir: Path, block: Mapping[str, Any], mode: str) -> list[str]:
    arms: Mapping[str, Any] = block["arms"]
    curves: Mapping[str, Any] = block["curves"]
    note = f"{mode} measurements only"
    lines = []
    specs = (
        ("executions", "Confirmed recall vs paired executions", "paired executions (k)"),
        ("usd", "Confirmed recall vs dollars per episode", "USD per episode"),
        ("wall_ms", "Confirmed recall vs elapsed time", "episode wall time (ms)"),
    )
    for key, title, xlabel in specs:
        series = [(a, [(float(x), y) for x, y in c[key]]) for a, c in curves.items() if c[key]]
        missing = [a for a, c in curves.items() if not c[key]]
        name = _write(
            out_dir / f"{prefix}_recall_vs_{key}.svg",
            line_chart(f"{title} ({mode})", xlabel, "confirmed recall", series, note=note),
        )
        lines.append(f"![{title}]({name})")
        if missing:
            lines.append(
                f"\n{title}: no curve for {', '.join(missing)} "
                "(a detected run lacks this measurement; not imputed)."
            )
    names = list(arms)
    fp = [arms[a]["clean_fp_rate"]["estimate"] or 0.0 for a in names]
    inc = [arms[a]["inconclusive_rate"] or 0.0 for a in names]
    name = _write(
        out_dir / f"{prefix}_fp_inconclusive.svg",
        bar_chart(
            f"Clean false-positive and inconclusive rates ({mode})",
            "rate",
            names,
            [("clean FP rate", fp), ("inconclusive rate", inc)],
            note=note,
        ),
    )
    lines.append(f"![False-positive and inconclusive rates]({name})")
    timing = [
        (t.removesuffix("_ms"), [arms[a]["timing_component_ms_mean"][t] or 0.0 for a in names])
        for t in TIMING_LABELS
    ]
    name = _write(
        out_dir / f"{prefix}_time_breakdown.svg",
        bar_chart(
            f"Mean time per episode by component ({mode})",
            "ms",
            names,
            timing,
            stacked=True,
            note=note,
        ),
    )
    lines.append(f"![Component time breakdown]({name})")
    return lines


def _split_section(
    mode: str, split: str, block: Mapping[str, Any], scores: Mapping[str, Any], out_dir: Path
) -> list[str]:
    arms: Mapping[str, Any] = block["arms"]
    budgets = [str(b) for b in scores["budgets"]]
    lines = [f"### {mode} / {split} split", ""]
    if block["descriptive_only"]:
        lines += [
            f"Small sample ({block['defect_cases']} defect cases): results are "
            "descriptive only; no superiority claim.",
            "",
        ]
    lines += [
        "Recall: share of defect cases with a label-matched confirmed witness within k "
        "pairs, mean over seeds, 95% percentile bootstrap grouped by defect family "
        f"({scores['bootstrap'].get('resamples')} resamples, seed "
        f"{scores['bootstrap'].get('seed')}).",
        "",
    ]
    lines += _table(
        [
            "arm",
            "runs",
            *[f"recall@{b}" for b in budgets],
            "precision",
            "clean FP runs",
            "inconclusive",
            "censored defect runs",
            "median pairs (detected only)",
        ],
        [
            [
                a,
                _f(s["runs"]),
                *[_ci(s["recall"][b]["grouped"]) for b in budgets],
                _f(s["precision"]),
                f"{s['clean_false_positive_runs']}/{s['clean_runs']}",
                f"{s['inconclusive_runs']}/{s['runs']}",
                f"{s['censored_defect_runs']}/{s['defect_runs']} (at {s['censored_at_pairs']})",
                _f(s["median_pairs_to_witness_detected_only"], 1),
            ]
            for a, s in arms.items()
        ],
    )
    lines += ["Case-level (ungrouped) recall intervals:", ""]
    lines += _table(
        ["arm", *[f"recall@{b}" for b in budgets]],
        [[a, *[_ci(s["recall"][b]["case"]) for b in budgets]] for a, s in arms.items()],
    )
    lines += ["Speed and cost (unknown costs are not counted as zero):", ""]
    lines += _table(
        [
            "arm",
            "p50/p95 wall ms",
            "p50 time to witness ms (detected)",
            "cold/warm p50 ms",
            "base/head/confirm/failed",
            "known cost",
            "unknown-cost runs",
            "$/case",
            "$/confirmed defect",
            "confirmation rate",
        ],
        [
            [
                a,
                f"{_f(s['wall_ms']['p50'], 1)}/{_f(s['wall_ms']['p95'], 1)}",
                _f(s["time_to_first_witness_ms_detected_only"]["p50"], 1),
                f"{_f(s['cold_start_wall_ms_p50'], 1)}/{_f(s['warm_wall_ms_p50'], 1)}",
                "/".join(str(s["invocations"][k]) for k in ("base", "head", "confirm", "failed")),
                _usd(s["known_cost_usd"]),
                _f(s["unknown_cost_runs"]),
                _usd(s["usd_per_case"]),
                _usd(s["usd_per_confirmed_defect"]),
                _f(s["confirmation_rate"]),
            ]
            for a, s in arms.items()
        ],
    )
    lines += ["Inconclusive and failed runs by reason:", ""]
    lines += _table(
        ["arm", "verdict:reason", "runs"],
        [
            [a, reason, str(n)]
            for a, s in arms.items()
            for reason, n in s["inconclusive_by_reason"].items()
        ]
        or [["-", "-", "0"]],
    )
    families = sorted({f for s in arms.values() for f in s["recall_by_family"]})
    if families:
        key = f"recall_at_{scores['max_pairs']}"
        lines += [f"Per-family recall@{scores['max_pairs']} (cases):", ""]
        lines += _table(
            ["arm", *families],
            [
                [
                    a,
                    *[
                        f"{_f(s['recall_by_family'][f][key])} ({s['recall_by_family'][f]['cases']})"
                        if f in s["recall_by_family"]
                        else "NA"
                        for f in families
                    ],
                ]
                for a, s in arms.items()
            ],
        )
    if block["paired"]:
        lines += ["Paired differences (a - b) on identical (case, seed, repetition) episodes:", ""]
        rows = []
        for p in block["paired"]:
            for b in budgets:
                r = p[f"recall_at_{b}"]
                rows.append(
                    [
                        f"{p['a']} - {p['b']}",
                        f"recall@{b}",
                        _ci(r["grouped"]),
                        _ci(r["case"]),
                        str(r["shared_episodes"]),
                    ]
                )
            fp = p["clean_fp_rate"]
            rows.append(
                [
                    f"{p['a']} - {p['b']}",
                    "clean FP rate",
                    "-",
                    _ci(fp["case"]),
                    str(fp["shared_episodes"]),
                ]
            )
            rows.append(
                [
                    f"{p['a']} - {p['b']}",
                    "cost USD",
                    "-",
                    _ci(p["cost_usd"]["case"]),
                    str(p["cost_usd"]["shared_episodes"]),
                ]
            )
        lines += _table(["contrast", "metric", "grouped 95% CI", "case 95% CI", "episodes"], rows)
    if block["factorial"]:
        lines += ["Factorial (four Qwen cells): main effects and interaction on recall:", ""]
        rows = [
            [f"recall@{b}", name, _ci(eff["grouped"]), _ci(eff["case"])]
            for b in budgets
            for name, eff in block["factorial"][f"recall_at_{b}"].items()
            if name != "shared_episodes"
        ]
        lines += _table(["metric", "effect", "grouped 95% CI", "case 95% CI"], rows)
    lines += ["Replicate spread (recall at max budget, per preregistered seed):", ""]
    lines += _table(
        ["arm", "per-seed recall", "spread"],
        [
            [
                a,
                ", ".join(_f(v) for v in s["replicate_recall_at_max"].values()),
                _f(s["replicate_spread"]),
            ]
            for a, s in arms.items()
        ],
    )
    lines += _figures(f"{mode}_{split}", out_dir, block, mode)
    return [*lines, ""]


def _ledger_section(mode: str, ledger: Sequence[Mapping[str, Any]], out_dir: Path) -> list[str]:
    rows = [t for t in ledger if t["measurement_mode"] == mode]
    if not rows:
        return [f"No ledger entries recorded for {mode} measurements.", ""]
    lines = [f"Cost/timing ledger ({mode} only; includes failed and retried calls):", ""]
    lines += _table(
        [
            "category",
            "phase",
            "entries",
            "paid",
            "measured",
            "estimated",
            "unknown-cost entries",
            "wall ms",
            "providers",
        ],
        [
            [
                t["category"],
                t["phase"],
                str(t["entries"]),
                str(t["paid_entries"]),
                _usd(t["measured_usd"]),
                _usd(t["estimated_usd"]),
                str(t["unknown_cost_entries"]),
                _f(t["wall_ms"], 0),
                ", ".join(t["providers"]),
            ]
            for t in rows
        ],
    )
    categories = sorted({t["category"] for t in rows})
    phases = sorted({t["phase"] for t in rows})
    series = [
        (
            p,
            [
                sum(
                    t["measured_usd"] + t["estimated_usd"]
                    for t in rows
                    if t["category"] == c and t["phase"] == p
                )
                for c in categories
            ],
        )
        for p in phases
    ]
    name = _write(
        out_dir / f"{mode}_cost_breakdown.svg",
        bar_chart(
            f"Known cost by component ({mode}; unknown excluded)",
            "USD",
            categories,
            series,
            stacked=True,
        ),
    )
    return [*lines, f"![Component cost breakdown]({name})", ""]


def render_report(scores: Mapping[str, object], out_dir: Path) -> Path:
    data = cast(Mapping[str, Any], scores)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scores.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    modes: Mapping[str, Any] = data["modes"]
    ledger: Sequence[Mapping[str, Any]] = data.get("ledger") or []
    present = [
        m for m in MODE_ORDER if m in modes or any(t["measurement_mode"] == m for t in ledger)
    ]
    lines = [
        "# ProofHunter research benchmark report",
        "",
        f"Protocol `{data['protocol_version']}`; implementation SHA(s): "
        f"{', '.join(f'`{s}`' for s in data.get('implementation_shas', [])) or 'none'}.",
        "",
        "Offline, recorded and live measurements are reported in separate sections and are "
        "never pooled. Misses are right-censored at the pair budget; medians of pairs to "
        "witness condition on detection. Track A claims apply only to the supported "
        "checkout contract under the frozen budget and pool.",
        "",
        f"Modes present: {', '.join(present) or 'none'}.",
        "",
    ]
    for mode in present:
        lines += [f"## {mode.capitalize()} measurements", "", MODE_NOTES[mode], ""]
        for split, block in sorted(modes.get(mode, {}).items()):
            lines += _split_section(mode, split, block, data, out_dir)
        lines += _ledger_section(mode, ledger, out_dir)
    report = out_dir / "report.md"
    report.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report
