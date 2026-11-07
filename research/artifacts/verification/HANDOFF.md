# Verification lane — phase 1 handoff

- Setup SHA rebased onto: `cadc5b4d91828fec1adcb5341a11c2c466e57c1f` (schemas/interfaces from `13b96671`).
- Branch: `devin/1794090000-research/verification` (no PR). Only owned paths touched:
  `research/verification/**`, `tests/research/verification/**`, `research/artifacts/verification/`.
- Checks derived only from the plan, RESEARCH-SPEC.md, frozen schemas, protocol-v1.json and
  spend-policy.json; no other lane's code was read. Nothing executes dataset/target-repo code.

## Audits (`python -m research.verification [roots...] [--reported m.json] [--live-teardown]`)
Artifacts are discovered under `research/artifacts/` by `schema_version`, so no lane layout is assumed.

| Gate | Module | Checks |
| --- | --- | --- |
| production_frozen | cli | `production_lock_sha256` of protocol vs files on disk |
| schemas_frozen / schema:* | schema | exported JSON Schema == model render; every row validated by Pydantic model **and** JSON Schema (Draft 2020-12) |
| split_leakage | splits | case_id, family_id, defect_origin, near_dup_cluster, template_id, (template, params) and head-source hash disjoint across train/validation/test; duplicate base/head pairs; reuse of frozen-bench source hashes; changed-line 5-token shingle Jaccard >= 0.9 across splits; per-split family/defect/clean targets |
| row_splits:* / test_label_unreachable | splits | observations and generator examples only use train/validation cases; test-only ids/hashes absent from every training/validation artifact byte |
| split_leakage_track_b | splits | repo/split_group/defect_origin disjoint; every task `executed=false`, `unverified_design_only` |
| label_provenance | provenance | independent `checkout-rounding@1` oracle (cross-checked vs production T1 oracle) recomputes witness expected value, input hash, kind (introduced/existing/clean), verdict; seal state, manifest binding, one label per case |
| observation_provenance / generator_example_provenance | provenance | outcome and relevance re-derived from oracle + recorded integers; forbidden-prefix features; exploration probability; generator inputs free of template/family identity; targets grounded in reproduced witnesses |
| budget_fairness | budget | <=6 distinct pairs, <=1 confirmation and only after a mismatch, <=2 retries, <=15 invocations, invocation counts vs probes, first-witness/censoring, verdict support (no_defect needs >=3 distinct matches), registered seeds, Snap disabled, identical pool hash across shared-pool arms per (mode, case, seed), frozen model refs per arm, single implementation SHA, optional full grid |
| metric_recomputation | metrics | recall@1/3/6 (case-averaged), precision, clean FP rate, inconclusive rate and reasons, confirmation rate, censored count, uncensored median pairs, wall p50/p95, cost/case and cost/confirmed defect (null if any cost unknown), per-family recall, family-cluster bootstrap CI and paired diff vs `builtin_eig`; every reported number compared |
| cost_ledger | ledger | cost null iff unknown, approved provider, pricing ref/date, pre-launch estimate, >50 USD check-in, >125% overrun, ledger vs raw-row cost by run |
| teardown_ledger / teardown_live | teardown | every paid resource has a teardown_verified entry; final verification after last paid entry per provider; optional read-only listing of Runpod pods/network volumes, Fireworks deployments/SFT jobs, OpenAI batches |
| checkpoint_hashes / deterministic_load | checkpoints | file/dir sha256 vs frozen hashes; raw-row checkpoint refs must be supplied; two LightGBM loads give bit-identical seeded predictions |

## Commands and results (commit 53674644caaeaeb7340eb8025eb4543ba1228b90)
```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research   # 96 passed
UV_OFFLINE=1 uv run --project research --locked mypy --strict research tests/research                 # no issues, 23 files
UV_OFFLINE=1 uv run --project research --locked python -m research.schemas.export --check              # ok
PROOFHUNTER_PROFILE=offline uv run --locked ruff check .                                               # all checks passed
PROOFHUNTER_PROFILE=offline uv run --locked pytest -q                                                  # 1273 passed
PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration                     # no issues, 81 files
PROOFHUNTER_PROFILE=offline uv run --locked python tools/gate.py setup                                 # PASSED
PROOFHUNTER_PROFILE=offline uv run --locked python tools/gate.py integrated                            # PASSED
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked python -m research.verification  # exit 1 (expected)
```
The audit exits 1 only because there are no lane artifacts yet (`split_leakage: no_manifest`); see `report.md`.
Root commands need network once: `mypy==2.4.1` was not in the offline uv cache.

## Artifacts, costs, limitations
- Dataset/model artifact hashes: none yet; no datasets or checkpoints are produced by this lane.
- Cost: 0 USD. No paid calls; the live teardown listing was not run (provider keys are not exposed in the shell).
- Near-duplicate check needs case base/head sources passed in (`sources=`); the CLI does not regenerate them, so this check only warns until phase 2.
- LightGBM determinism is checked; QLoRA adapter determinism can only be checked by hash, since the reviewed GPU load path does not exist yet.
- Fireworks account id `peli` is hard-coded for listings.

## Remaining dependencies (phase 2)
Integrated SHA, CaseManifest + LabelFiles, observations, generator examples, raw results, ledger,
reported metrics JSON, checkpoint hashes and the reviewed loader/evaluation entrypoint for reproduction;
provider keys in the environment for `--live-teardown`.
