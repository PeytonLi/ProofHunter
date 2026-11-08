# ruff: noqa: E501, E731, B905
"""Build the per-metric comparison graphs (diagram-design editorial skin) from committed R1 validation reports."""

import json
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
R1 = ROOT / "execution/reports/r1-validation"
OUT = Path(__file__).resolve().parent / "graphs.html"

PAPER, INK, MUTED, SOFT, ACCENT = "#f5f5f5", "#2d3142", "#4f5d75", "#5a6580", "#bf4520"
MONO, SANS = "'Geist Mono', ui-monospace, monospace", "'Geist', system-ui, sans-serif"

LABEL = {
    "frontier_agent": "Frontier agent (gpt-6.1-sol)",
    "frontier_reviewer": "Frontier reviewer (gpt-6.1-sol)",
    "simple_heuristic": "Simple heuristic",
    "learned_ranker": "LightGBM ranker",
    "uniform_random": "Uniform random",
    "builtin_eig": "Production EIG",
    "fixed_order": "Fixed order",
    "learned_ranker_no_belief": "LightGBM ranker, no belief",
}


def load_arms():
    arms = {}
    for f in ["local", "ranker", "frontier_reviewer", "frontier_agent"]:
        d = json.loads((R1 / f"{f}-pilot-report.json").read_text())
        for a, v in d["arms"].items():
            v["degraded"] = sum(d["degraded_by_arm"].get(a, {}).values())
            arms[a] = v
    scores = json.loads((R1 / "report/scores.json").read_text())["modes"]
    for mode in scores.values():
        for a, s in mode["validation"]["arms"].items():
            arms[a]["ci6"] = s["recall"]["6"]["case"]
            arms[a]["usd_per_confirmed"] = s.get("usd_per_confirmed_defect")
    return arms


def t(x, y, s, size=9, fill=MUTED, anchor="start", font=MONO, weight=400, extra=""):
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" font-family="{font}" '
        f'font-weight="{weight}" text-anchor="{anchor}" {extra}>{escape(str(s))}</text>'
    )


def hbar(rows, fmt, xmax, focal, unit, ci=None, note=""):
    """rows: [(label, value)] sorted; focal: label key to accent."""
    W, left, right, rowh, top = 1000, 260, 120, 34, 30
    H = top + rowh * len(rows) + 40
    span = W - left - right
    s = [f'<rect width="{W}" height="{H}" fill="{PAPER}"/>']
    for i in range(5):
        gx = left + span * i / 4
        s.append(
            f'<line x1="{gx:.1f}" y1="{top - 8}" x2="{gx:.1f}" y2="{H - 34}" stroke="rgba(45,49,66,0.08)" stroke-width="0.8"/>'
        )
        s.append(t(gx, H - 20, fmt(xmax * i / 4), 8, anchor="middle"))
    s.append(
        t(left + span / 2, H - 6, unit.upper(), 7, anchor="middle", extra='letter-spacing="0.14em"')
    )
    for i, (key, v) in enumerate(rows):
        y = top + i * rowh
        f = key == focal
        stroke = ACCENT if f else MUTED
        fill = "rgba(191,69,32,0.10)" if f else "rgba(79,93,117,0.15)"
        w = max(span * v / xmax, 1.5)
        s.append(
            t(
                left - 12,
                y + 15,
                LABEL.get(key, key),
                12,
                INK if f else MUTED,
                "end",
                SANS,
                600 if f else 500,
            )
        )
        s.append(
            f'<rect x="{left}" y="{y + 2}" width="{w:.1f}" height="20" fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
        )
        if ci and key in ci:
            lo, hi = ci[key]
            x1, x2 = left + span * lo / xmax, left + span * hi / xmax
            s.append(
                f'<line x1="{x1:.1f}" y1="{y + 12}" x2="{x2:.1f}" y2="{y + 12}" stroke="{INK}" stroke-width="1"/>'
            )
            for xx in (x1, x2):
                s.append(
                    f'<line x1="{xx:.1f}" y1="{y + 7}" x2="{xx:.1f}" y2="{y + 17}" stroke="{INK}" stroke-width="1"/>'
                )
        s.append(
            t(
                left + w + (max(span * ci[key][1] / xmax - w, 0) if ci and key in ci else 0) + 8,
                y + 15,
                fmt(v),
                9,
                ACCENT if f else MUTED,
            )
        )
    return W, H, "".join(s)


def lines(series, focal):
    """series: {key: [r1, r3, r6]} recall vs executions budget."""
    W, H, left, right, top, bot = 1000, 420, 70, 260, 30, 60
    sx = lambda b: left + (W - left - right) * (b - 1) / 5
    sy = lambda v: top + (H - top - bot) * (1 - v)
    s = [f'<rect width="{W}" height="{H}" fill="{PAPER}"/>']
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        s.append(
            f'<line x1="{left}" y1="{sy(v):.1f}" x2="{W - right}" y2="{sy(v):.1f}" stroke="rgba(45,49,66,0.08)" stroke-width="0.8"/>'
        )
        s.append(t(left - 8, sy(v) + 3, f"{v:.0%}", 8, anchor="end"))
    for b in (1, 3, 6):
        s.append(t(sx(b), H - bot + 18, f"{b}", 8, anchor="middle"))
    s.append(
        t(
            (left + W - right) / 2,
            H - 14,
            "EXECUTION BUDGET (PAIRED BASE/HEAD PROBES)",
            7,
            anchor="middle",
            extra='letter-spacing="0.14em"',
        )
    )
    s.append(
        t(
            18,
            sy(0.5),
            "CONFIRMED RECALL",
            7,
            anchor="middle",
            extra=f'letter-spacing="0.14em" transform="rotate(-90 18 {sy(0.5):.1f})"',
        )
    )
    ends = sorted(series.items(), key=lambda kv: -kv[1][-1])
    last_y = -99
    for key, vals in sorted(series.items(), key=lambda kv: kv[0] == focal):
        f = key == focal
        pts = " ".join(f"{sx(b):.1f},{sy(v):.1f}" for b, v in zip((1, 3, 6), vals))
        c = ACCENT if f else MUTED
        s.append(
            f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="{2 if f else 1}" opacity="{1 if f else 0.7}"/>'
        )
        for b, v in zip((1, 3, 6), vals):
            s.append(
                f'<circle cx="{sx(b):.1f}" cy="{sy(v):.1f}" r="{3.2 if f else 2.4}" fill="{PAPER}" stroke="{c}" stroke-width="1.2"/>'
            )
    for key, vals in ends:
        y = max(sy(vals[-1]) + 3, last_y + 13)
        last_y = y
        f = key == focal
        s.append(
            t(
                sx(6) + 12,
                y,
                f"{LABEL[key]}  {vals[-1]:.0%}",
                10,
                ACCENT if f else MUTED,
                font=SANS,
                weight=600 if f else 500,
            )
        )
    return W, H, "".join(s)


def scatter(points, focal):
    """points: {key: (usd_per_run, recall6)} with log-ish x via sqrt."""
    W, H, left, right, top, bot = 1000, 420, 80, 60, 30, 60
    xmax = 0.012
    sx = lambda v: left + (W - left - right) * v / xmax
    sy = lambda v: top + (H - top - bot) * (1 - v)
    s = [f'<rect width="{W}" height="{H}" fill="{PAPER}"/>']
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        s.append(
            f'<line x1="{left}" y1="{sy(v):.1f}" x2="{W - right}" y2="{sy(v):.1f}" stroke="rgba(45,49,66,0.08)" stroke-width="0.8"/>'
        )
        s.append(t(left - 8, sy(v) + 3, f"{v:.0%}", 8, anchor="end"))
    for v in (0, 0.003, 0.006, 0.009, 0.012):
        s.append(t(sx(v), H - bot + 18, f"${v:.3f}", 8, anchor="middle"))
    s.append(
        t(
            (left + W - right) / 2,
            H - 14,
            "MODEL SPEND PER EPISODE (USD, MEASURED)",
            7,
            anchor="middle",
            extra='letter-spacing="0.14em"',
        )
    )
    s.append(
        t(
            18,
            sy(0.5),
            "RECALL @ 6",
            7,
            anchor="middle",
            extra=f'letter-spacing="0.14em" transform="rotate(-90 18 {sy(0.5):.1f})"',
        )
    )
    zero = sorted([k for k, (c, _) in points.items() if c == 0], key=lambda k: -points[k][1])
    for key, (c, r) in points.items():
        f = key == focal
        col = ACCENT if f else MUTED
        s.append(
            f'<circle cx="{sx(c):.1f}" cy="{sy(r):.1f}" r="{5 if f else 4}" fill="{"rgba(191,69,32,0.15)" if f else "rgba(79,93,117,0.15)"}" stroke="{col}" stroke-width="1.2"/>'
        )
        if c > 0:
            s.append(t(sx(c) - 10, sy(r) + 4, LABEL[key], 10, col, "end", SANS, 600 if f else 500))
    for i, key in enumerate(zero):
        s.append(
            t(
                sx(0) + 12,
                sy(0.62) + 13 * i - 40
                if False
                else sy(points[key][1]) + 4
                if i == 0
                else sy(points[zero[0]][1]) + 4 + 13 * i,
                f"{LABEL[key]} {points[key][1]:.0%}",
                9,
                MUTED,
                font=SANS,
            )
        )
    return W, H, "".join(s)


def figure(n, title, sub, svg, takeaway):
    W, H, body = svg
    return f"""
<section>
  <p class="eyebrow">Metric {n:02d}</p>
  <h2>{escape(title)}</h2>
  <p class="sub">{escape(sub)}</p>
  <svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img"><title>{escape(title)}</title>{body}</svg>
  <p class="callout">{escape(takeaway)}</p>
</section>"""


def main():
    a = load_arms()
    order = sorted(a, key=lambda k: -a[k]["descriptive_recall_at"]["6"])
    pct = lambda v: f"{v:.0%}"
    figs = []
    rec = {k: [a[k]["descriptive_recall_at"][b] for b in ("1", "3", "6")] for k in a}
    figs.append(
        figure(
            1,
            "Confirmed recall vs. execution budget",
            "50 validation cases × 5 seeds, 230 defect runs per arm. Budgets 1, 3, 6 paired probes.",
            lines(rec, "frontier_agent"),
            "The tool-using frontier agent finds most defects on its first probe; no learned or search-based ranker gets close.",
        )
    )
    ci = {k: (a[k]["ci6"]["low"], a[k]["ci6"]["high"]) for k in a if "ci6" in a[k]}
    figs.append(
        figure(
            2,
            "Recall @ 6 with 95% case-bootstrap CI",
            "Whiskers: grouped case bootstrap (10,000 resamples). Ranker arms report point estimates only.",
            hbar(
                [(k, a[k]["descriptive_recall_at"]["6"]) for k in order],
                pct,
                1.0,
                "frontier_agent",
                "recall @ 6",
                ci,
            ),
            "Only the two frontier arms clear the simple heuristic. The LightGBM ranker is indistinguishable from random.",
        )
    )
    r1 = sorted(a, key=lambda k: -a[k]["descriptive_recall_at"]["1"])
    figs.append(
        figure(
            3,
            "Recall @ 1 — first-probe hit rate",
            "Share of defect runs confirmed by the very first paired probe.",
            hbar(
                [(k, a[k]["descriptive_recall_at"]["1"]) for k in r1],
                pct,
                1.0,
                "frontier_agent",
                "recall @ 1",
            ),
            "The heuristic doubles EIG at budget 1; the learned ranker is the worst arm at budget 1.",
        )
    )
    rk = json.loads((ROOT / "artifacts/ranker/real-v2/validation-comparison.json").read_text())[
        "arms"
    ]
    rlabel = {
        "simple_heuristic": "Simple heuristic",
        "v2_no_belief": "Ranker v2, no belief",
        "v1_dense_full": "Ranker v1 (frozen)",
        "v1_dense_no_belief": "Ranker v1, no belief",
        "v2_full": "Ranker v2 full",
        "uniform_random": "Uniform random",
        "v2_no_diff": "Ranker v2, no diff",
        "builtin_eig": "Production EIG",
        "fixed_order": "Fixed order",
    }
    LABEL.update(rlabel)
    rrows = sorted(((k, v["mrr"]) for k, v in rk.items() if k in rlabel), key=lambda kv: -kv[1])
    figs.append(
        figure(
            4,
            "Ranker validation MRR (747 decision points)",
            "LightGBM LambdaRank v1 and v2 (288-config validation sweep) vs. non-learned orderings.",
            hbar(rrows, lambda v: f"{v:.3f}", 0.6, "v2_no_belief", "mean reciprocal rank"),
            "Best learned ranker 0.366 vs. heuristic 0.557. v2 features did not close the gap — keep the heuristic, drop the learned ranker.",
        )
    )
    paid = [k for k in order if a[k]["cost_usd_total"] > 0]
    figs.append(
        figure(
            5,
            "Dollars per confirmed defect",
            "Measured OpenAI spend from the cost ledger; local arms cost $0 in model inference.",
            hbar(
                [(k, a[k]["usd_per_confirmed"]) for k in paid],
                lambda v: f"${v:.4f}",
                0.016,
                "frontier_agent",
                "usd per confirmed defect",
            ),
            "The agent costs ~3.5× the reviewer per confirmed defect but finds 13% more defects.",
        )
    )
    figs.append(
        figure(
            6,
            "Recall vs. cost",
            "Each point is one arm. X = measured model spend per episode.",
            scatter(
                {
                    k: (
                        a[k]["cost_usd_total"] / a[k]["episodes"],
                        a[k]["descriptive_recall_at"]["6"],
                    )
                    for k in a
                },
                "frontier_agent",
            ),
            "Roughly one cent per episode buys ~20 points of recall over the best free method.",
        )
    )
    lat = sorted(a, key=lambda k: a[k]["wall_ms_p50"])
    figs.append(
        figure(
            7,
            "Wall-clock latency, p50",
            "Per-episode wall time, including model calls for live arms.",
            hbar(
                [(k, a[k]["wall_ms_p50"] / 1000) for k in lat],
                lambda v: f"{v:.2f}s",
                4.0,
                "frontier_agent",
                "seconds (p50)",
            ),
            "Local arms finish in ~0.2–0.3 s; frontier arms add 1.7–2.1 s of model latency.",
        )
    )
    figs.append(
        figure(
            8,
            "Wall-clock latency, p95",
            "Tail latency per episode.",
            hbar(
                [
                    (k, a[k]["wall_ms_p95"] / 1000)
                    for k in sorted(a, key=lambda k: a[k]["wall_ms_p95"])
                ],
                lambda v: f"{v:.2f}s",
                10.0,
                "frontier_agent",
                "seconds (p95)",
            ),
            "The agent's multi-turn loop has a 9.3 s tail — the main operational cost of the winning arm.",
        )
    )
    figs.append(
        figure(
            9,
            "Executions used per episode",
            "Mean sandbox invocations (base + head + confirmation) per episode.",
            hbar(
                [
                    (k, a[k]["invocations_mean"])
                    for k in sorted(a, key=lambda k: a[k]["invocations_mean"])
                ],
                lambda v: f"{v:.1f}",
                12,
                "frontier_agent",
                "invocations per episode",
            ),
            "Better ordering also means fewer executions: the agent uses ~40% fewer than EIG.",
        )
    )
    figs.append(
        figure(
            10,
            "Degraded episodes (fallback used)",
            "Episodes where the arm fell back to fixed order; counted against the originating arm.",
            hbar(
                [
                    (k, a[k]["degraded"] / a[k]["episodes"])
                    for k in sorted(a, key=lambda k: -a[k]["degraded"])
                ],
                pct,
                1.0,
                "frontier_reviewer",
                "share of episodes degraded",
            ),
            "The reviewer returned an empty ranking in 40% of episodes (100/250) — its recall includes those fallbacks.",
        )
    )
    figs.append(
        figure(
            11,
            "Clean-control false positives",
            "Confirmed defects reported on clean controls (4 clean cases × 5 seeds = 20 runs per arm; small control set).",
            hbar(
                [(k, a[k]["clean_false_positives"]) for k in order],
                lambda v: f"{v:.0f}",
                4,
                None,
                "false-positive runs",
            ),
            "Zero false positives for every arm: the trusted oracle + confirmation rule holds regardless of the ranker.",
        )
    )
    stats = [
        ("46%", "untuned Qwen2.5-Coder-7B valid-plan parse rate (300 validation generations)"),
        (
            "0.069",
            "untuned Qwen validation score; precision 0 (every plan listed all hypothesis types)",
        ),
        (
            "42 / 2,000",
            "rounding-bug SFT examples in train — label mix too skewed for a strong tuned result",
        ),
    ]
    stat_html = "".join(
        f'<div class="stat"><div class="num">{escape(n)}</div><div class="cap">{escape(c)}</div></div>'
        for n, c in stats
    )
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ProofHunter research · metric comparisons</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:{SANS};background:{PAPER};color:{INK};padding:3rem 2rem}}
.frame{{max-width:1080px;margin:0 auto}}
.eyebrow{{font-family:{MONO};font-size:.66rem;font-weight:500;letter-spacing:.18em;text-transform:uppercase;color:{MUTED};margin-bottom:.4rem}}
h1{{font-family:'Instrument Serif',serif;font-size:2.4rem;font-weight:400;letter-spacing:-.02em;margin-bottom:.6rem}}
h2{{font-family:'Instrument Serif',serif;font-size:1.6rem;font-weight:400;letter-spacing:-.01em;margin-bottom:.2rem}}
.lede{{color:{MUTED};max-width:760px;line-height:1.55;margin-bottom:2.5rem}}
.sub{{font-family:{MONO};font-size:.72rem;color:{SOFT};margin-bottom:1rem}}
section{{border-top:1px solid rgba(45,49,66,.12);padding:2.2rem 0}}
svg{{width:100%;display:block}}
.callout{{font-family:'Instrument Serif',serif;font-style:italic;font-size:1.1rem;color:{MUTED};margin-top:.8rem}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;margin-top:1rem}}
.stat .num{{font-family:'Instrument Serif',serif;font-size:2.6rem;color:{INK}}}
.stat:first-child .num{{color:{ACCENT}}}
.stat .cap{{font-family:{MONO};font-size:.7rem;color:{MUTED};line-height:1.5}}
table{{border-collapse:collapse;font-size:.85rem;margin-top:1rem;width:100%}}
td,th{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid rgba(45,49,66,.12)}}
th{{font-family:{MONO};font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;color:{MUTED};font-weight:500}}
</style></head><body><div class="frame">
<p class="eyebrow">ProofHunter research track · Track A checkout contract · validation split</p>
<h1>Does learning help ProofHunter find defects?</h1>
<p class="lede">R1 validation pilot: 50 validation cases × 5 seeds per arm, identical shared candidate pools, 6-probe budget, trusted checkout-rounding oracle.
Frontier arms use gpt-6.1-sol with measured cost. Sealed test-set results are not included; these are validation results and may not generalize beyond the synthetic checkout task.</p>
{"".join(figs)}
<section><p class="eyebrow">Metric 12</p><h2>Qwen2.5-Coder-7B hypothesis generator</h2>
<p class="sub">QLoRA training stopped at the deadline before the first checkpoint; no tuned adapter exists. Pod deleted and verified.</p>
<div class="stats">{stat_html}</div></section>
<section><p class="eyebrow">Recommendation</p><h2>Simplify to a strong tool-using LLM; keep the heuristic as the free fallback</h2>
<table><tr><th>Option</th><th>Evidence</th><th>Verdict</th></tr>
<tr><td>Keep EIG</td><td>Recall@6 43%, below random (47%)</td><td>Replace</td></tr>
<tr><td>Learned LightGBM ranker</td><td>MRR 0.36 vs. heuristic 0.56; recall@6 48%</td><td>Do not ship</td></tr>
<tr><td>Fine-tuned Qwen generator</td><td>Untuned precision 0; skewed SFT labels</td><td>Not justified yet</td></tr>
<tr><td>Simple heuristic</td><td>Recall@6 57%, $0, 0.2 s</td><td>Free default</td></tr>
<tr><td>Tool-using frontier agent</td><td>Recall@6 76%, $0.014 per confirmed defect, p95 9.3 s</td><td>Best quality</td></tr></table></section>
</div></body></html>"""
    OUT.write_text(html)
    print(OUT)


if __name__ == "__main__":
    main()
