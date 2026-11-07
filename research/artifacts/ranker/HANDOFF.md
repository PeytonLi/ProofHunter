# Ranker lane handoff (LANE=ranker), real data v1

- Branch `devin/1794090000-research/ranker`, on setup `cadc5b4d91828fec1adcb5341a11c2c466e57c1f`
  (schemas/interfaces `13b9667`). Dataset: branch `devin/1794090000-research/dataset` @ `b48a61f9`,
  manifest `ba3d927d994c4c54944ab42a03b2b253ac97bbfa0193fca37e28b22c87b21520`, public tarball sha256
  `046c59ca78329a9f31ea98d196bf95cd24571cc69f38b030851e5b47b151da98`. No PR, no merge, no edits
  outside `research/ranker/**`, `tests/research/ranker/**`, `research/artifacts/ranker/**`.
- Test labels and test rows were never read (collection, re-featurization, training and comparison
  all refuse `split=test`; `labels/*.json` were not opened).

## Frozen checkpoint (primary)

`real-v1/checkpoints/dense_full/checkpoint.json` sha256
**`cc02fb161aa7689438d1329914c447bda7cf943e6165d0941b49f145fb6e5174`** (LightGBM 4.7.0, seed
20261107, params `num_leaves=7, learning_rate=0.05, min_data_in_leaf=20, num_boost_round=150`,
selected on validation). Retraining from the same data reproduced the identical hash.
Load with `load_ranker_selector(<dir or checkpoint.json>, arm_id="builtin_ranker")`.
No-belief ablation: `dense_no_belief` `d57c4fc0fdce36e5ae83e77a3198a59262c54ae121ebd6f56fea48700cdd8af1`.

## Designs (see `research/ranker/FEATURES.md`)

| checkpoint | train data | groups | weights |
|---|---|---|---|
| dense_full / dense_no_belief | dense full-pool collection, `eig_eps0.3`, seed 1633385816 | decision point | none |
| logged_ips_full / logged_ips_no_belief | dataset `eps0.3-heuristic-v1` log, re-featurized | episode | IPS, clip 50, mean 1 |
| logged_unweighted_full | same log | episode | none |

Validation for tuning and comparison: dense validation decision points (747 with a positive).

## Validation results (`real-v1/validation-comparison.json`)

| arm | MRR | hit@1 |
|---|---|---|
| simple_heuristic (spec arm) | 0.557 | 0.452 |
| dense_full (primary) | 0.358 | 0.245 |
| logged_unweighted_full | 0.355 | 0.214 |
| dense_no_belief | 0.351 | 0.249 |
| logged_ips_no_belief | 0.345 | 0.173 |
| uniform_random (exact) | 0.343 | 0.174 |
| logged_ips_full | 0.339 | 0.186 |
| builtin_eig | 0.316 | 0.170 |
| fixed_order | 0.303 | 0.236 |

Primary minus arm, case-clustered bootstrap 95% CI (2,000 resamples, 360 cases):
heuristic −0.198 [−0.263, −0.136]; random +0.016 [−0.037, +0.072]; EIG +0.042 [−0.024, +0.110];
fixed +0.055 [−0.027, +0.137]; no_belief +0.007 [−0.013, +0.026]; logged IPS +0.020 [−0.035, +0.076].

**Negative result.** No learned ranker beats uniform random or EIG with a CI that excludes 0, and all
lose clearly to the non-learned simple heuristic. Removing belief features changes nothing
measurable. IPS weighting did not help the logged design. Train and validation use disjoint defect
families, and the model fits train-family cues that do not transfer.

## Data hashes (not committed; regenerate with the commands below)

| file | rows | sha256 (file) | stable sha256 (excl. wall_ms; = checkpoint `*_sha256`) |
|---|---|---|---|
| dense/train.jsonl | 439,377 (12,612 unknown) | `56694321…3efc` | `10ac81eb3a014903d3374278e5f3e4c61fcc22317c4d960ef0fbae11ed43da14` |
| dense/validation.jsonl | 68,482 (1,099 unknown) | `be7d8dca…7a` | `c2ea1b1fbd60094a8ba4895b38f5f50dc864d4aee487cbbe08a8fb763218dbab` |
| logged/train.jsonl (re-featurized) | 32,129 (1,059 unknown) | `7d07656d…7743` | `103af9e33122609532c9ca1605fe19ffaa65c182e242d6273322f3a25d5c4e30` |

```
R=<extracted tarball>/track_a   # needs research/data from the dataset branch on PYTHONPATH/in tree
uv run --project research --locked python -m research.cli collect-observations --store dataset --root $R \
  --split train|validation --seeds 1633385816 --policy eig_eps0.3 --workers 6 --out dense/<split>.jsonl
uv run --project research --locked python -m research.cli train-ranker --train dense/train.jsonl \
  --validation dense/validation.jsonl --out ckpt/dense --ablation full --ablation no_belief
uv run --project research --locked python -m research.cli train-ranker --train $R/observations/train.jsonl \
  --validation dense/validation.jsonl --store dataset --root $R --group-by episode --weighting ips --out ckpt/logged_ips
uv run --project research --locked python -m research.ranker.compare --validation dense/validation.jsonl \
  --checkpoint dense_full=ckpt/dense/full ... --primary dense_full --out validation-comparison.json
```

## Tests

```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research
UV_OFFLINE=1 uv run --project research --locked python -m research.schemas.export --check
UV_OFFLINE=1 uv run --project research --locked mypy --strict research tests/research
UV_OFFLINE=1 uv run --project research --locked ruff check research/ranker tests/research/ranker
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check .
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python tools/gate.py setup|integrated
```

## Costs

No paid calls (no ledger entries). Local CPU only, 8 vCPU: dense collection ≈ 9 min (train, 6
workers) + 3 min (validation, 2 workers); re-featurization 6 min; training ≈ 4 min per dense
ablation (peak RSS 6.1 GB), < 1 min per logged run; comparison < 1 min.

## Limitations / dependencies

- Dense collection uses one replicate seed and executes the whole guarded pool per step (ranker
  data collection, not the frozen 6-pair evaluation budget; the integration lane enforces that).
- Metric is single-decision ranking on validation, not episode metrics; the integration and
  measurement lanes own episode-level and test results.
- Pool conventions differ between lanes: the protocol/integration pool is
  `bayes.generate(plan, candidate_seed)` (used by dense collection and at inference), while the
  dataset log uses `pool_seed(candidate_seed, seed)`. Re-featurization follows the dataset's.
- Missing coverage in ranker states comes from usable-history coverage tags, not
  `oracle.compute_coverage`; the integration runner should confirm equivalence.
- Logged episode grouping leaks the stopping rule through `step`/history features (see FEATURES.md).
- Fixture artifacts from the first handoff remain in `fixture-v1/` (unchanged).
