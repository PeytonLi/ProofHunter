# ProofHunter research track — deadline report (validation only)

Every number is read from committed artifacts:
`research/execution/reports/r1-validation/*-pilot-report.json`, `.../report/scores.json`, and
`research/artifacts/ranker/real-v2/validation-comparison.json`.

## Setup
Track A (supported checkout contract), validation split, 50 cases (46 defect, 4 clean) × 5 seeds,
6 paired-probe budget, trusted checkout-rounding oracle, identical candidate pools per arm.

## Results (R1 validation pilot)
| Arm | Recall@1 | Recall@6 (95% CI) | Clean FP | $ / confirmed defect | p50 / p95 wall | Invocations |
|---|---|---|---|---|---|---|
| Frontier agent (gpt-6.1-sol) | 0.73 | 0.76 (0.60–0.87) | 0 | 0.0145 | 1.69 s / 9.34 s | 5.7 |
| Frontier reviewer (gpt-6.1-sol) | 0.67 | 0.67 | 0 | 0.0044 | 2.09 s / 3.92 s | 6.4 |
| Simple heuristic | 0.52 | 0.57 | 0 | 0 | 0.20 s / 0.33 s | 7.5 |
| LightGBM ranker | 0.13 | 0.48 | 0 | 0 | 0.28 s / 0.43 s | 10.0 |
| Uniform random | 0.20 | 0.47 | 0 | 0 | 0.28 s / 0.47 s | 9.1 |
| Production EIG | 0.24 | 0.43 | 0 | 0 | 0.24 s / 0.35 s | 9.4 |
| Fixed order | 0.24 | 0.43 | 0 | 0 | 0.27 s / 0.39 s | 9.5 |
| LightGBM ranker, no belief | 0.15 | 0.30 | 0 | 0 | 0.28 s / 0.35 s | 9.8 |

Exact CIs per arm are in `scores.json`. The reviewer degraded (empty ranking → fixed-order fallback) in 100/250 episodes;
those count against it.

Ranker validation MRR (747 decision points): heuristic 0.557, best learned (v2 no-belief) 0.366, v1 0.358, random 0.343, EIG 0.316.

## Recommendation
Simplify to a tool-using frontier LLM agent for quality; keep the simple heuristic as the free fallback. Replace EIG
(below random). Do not ship the LightGBM ranker. Qwen fine-tuning is not justified by available evidence
(untuned: 46% parse rate, score 0.069, precision 0; SFT set has 42/2,000 rounding examples).

## Not done / caveats
- Sealed test-set evaluation not run; all results are validation.
- No tuned Qwen adapter exists: training was stopped at the deadline 51 minutes into epoch 1 of 3, before the first epoch-end checkpoint. Runpod pod `mozp9af8wlaf9h` was deleted and verified gone (1.14 h, about $1.02 estimated). See PR #12 and `research/artifacts/generator/full-qlora-v1/teardown-evidence.json`.
- Frontier 4-model × 3-role grid ran only as a 3-case smoke; not reported as a result.
- Only 4 clean controls in the pilot; false-positive evidence is weak.
- Track B (SWE-smith) is metadata-only; no target code executed.
- ASI:One live demo not complete (basic Daytona canary only, PR #4).
- Results come from a synthetic checkout task and do not generalize to arbitrary repositories.
