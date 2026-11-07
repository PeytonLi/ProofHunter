# Ranker lane handoff (LANE=ranker)

- Branch: `devin/1794090000-research/ranker`, rebased on setup `cadc5b4d91828fec1adcb5341a11c2c466e57c1f`
  (schemas/interfaces `13b9667`). No PR, no merge. No edits outside owned paths.
- Owned files: `research/ranker/{__init__,features,collect,executor,fixture,metrics,baselines,train,selector,commands}.py`,
  `research/ranker/FEATURES.md` (feature spec), `tests/research/ranker/test_ranker.py`,
  `research/artifacts/ranker/**` (force-added; `research/.gitignore` ignores `artifacts/`).

## Interfaces implemented (OWNERSHIP.md)
- `features.FEATURE_SET_VERSION = "ranker-features-v1"`, `CheckoutFeatures` (structural `I.FeatureExtractor`), 68 pre-execution features in 6 groups; ablations `full`, `no_belief`, `no_belief_no_predictor`.
- `collect.collect_observations(store, split, seeds, policy)`: train/validation only. Runs **reviewed sources only**: the default reviewer requires matching hashes, a reviewed `research.data.templates.list_templates()` entry and a byte-identical `templates.render(template_id, params)`. Execution is in a `python -I -S` child process with an empty environment and rlimits. Every guarded pool candidate is executed at each decision step, so each decision point gives a real LambdaRank group. Policies: `eig_eps<e>` (ε-greedy production EIG) and `uniform_random`. Rows record `selection_probability`.
- `train.LambdaRankTrainer(ablation).train(train, validation, out_dir) -> sha256(checkpoint.json)`: LightGBM 4.7.0 (research lock), `deterministic`, 1 thread, fixed seed 20261107. 24-point grid chosen by validation MRR only. `unknown` rows are dropped, never counted as zero. Rows from the wrong split raise `PermissionError`.
- `selector.load_ranker_selector(checkpoint, *, arm_id)`: takes `checkpoint.json` (the integration lane's frozen file hash) or its directory. Checks the version, feature names and the model.txt hash, and exposes `checkpoint_sha256`. Ties break on input hash.
- `commands.collect_observations` / `commands.train_ranker`: `--store fixture|dataset --root` (dataset uses `research.data.store.open_store`).
- Labels are never read: `FixtureStore.labels` raises and the tests assert there were zero reads.

## Tests (all pass at the handoff commit)
```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research   # 86 passed, 2 skipped
UV_OFFLINE=1 uv run --project research --locked python -m research.schemas.export --check             # exit 0
UV_OFFLINE=1 uv run --project research --locked mypy --strict research tests/research                  # no issues (19 files)
UV_OFFLINE=1 uv run --project research --locked ruff check research/ranker tests/research/ranker       # clean
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q                                      # 1252 passed, 3 skipped
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check .                                   # clean
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration         # no issues
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python tools/gate.py setup|integrated          # PASSED / PASSED
```

## Artifacts (fixture only; `fixture-v1/SHA256SUMS`)
The data comes from the synthetic fixture store (24 cases, 6 families, grouped splits; family shift between train and validation). Collection used policy `eig_eps0.3` with the 5 protocol seeds. Train: 8,732 rows, 193 groups. Validation: 7,332 rows, 191 groups (88 with a positive).

| file | sha256 |
|---|---|
| data/train.jsonl.gz (raw stream sha `f712cb40…51f1`) | `095ae2a620c76d0d7dae0ac03d6982b5042a74e27906f26eb69ad5a600fcc7c1` |
| data/validation.jsonl.gz (raw `7bd845ca…d0b0`) | `42aee0439799415926f37f19007a48e8488b469149c3ed242fa04ed48931bd5c` |
| checkpoints/full/checkpoint.json | `5d2bfd52a028643088b8711176244487de9c18735734ae4ddbf026565cc95496` |
| checkpoints/no_belief/checkpoint.json | `9ae4e3fee084f5f03e252ed43219cb809edca854a7f4d4192bdc11cbdd6dd927` |
| checkpoints/no_belief_no_predictor/checkpoint.json | `3dc070a59de2659e19d966f5c18735e47512e21eefaf5b1ce61b4ca699a32c52` |

Fixture validation results (decision-point first-positive rank, 88 groups):

| arm | MRR | hit@1 | hit@3 |
|---|---|---|---|
| LambdaRank full | 0.192 | 0.057 | 0.295 |
| LambdaRank no_belief | 0.198 | 0.057 | 0.227 |
| LambdaRank no_belief_no_predictor | 0.194 | 0.057 | 0.148 |
| production EIG score (`eig_bits`) | 0.178 | 0.125 | 0.125 |
| uniform random (exact expectation) | 0.283 | 0.128 | 0.318 |
| best single feature `lines_with_fraction` (picked on validation, so optimistic) | 0.453 | 0.352 | 0.523 |

On the fixture, the ranker does not beat random (negative result). The train and validation families are deliberately disjoint, and the ranker overfits train-family cues. The fixture is too small to support any claim.

## Costs
- Paid calls: none, so there are no ledger entries. The CPU time was local: each collection took about 60 s, and training all 3 ablations took about 10 s.
- Row `wall_ms` is the batch wall time divided per invocation. It is not a per-call measurement.

## Known limitations / remaining dependencies
- **Real data not trained yet.** `devin/1794090000-research/dataset` did not exist at handoff. When it lands: `collect-observations --store dataset --root <root> --split train|validation --out …`, then `train-ranker …`, then freeze `checkpoint.json` + hash in the integration config.
- The default reviewer depends on the dataset lane's `templates.list_templates`/`render` as specified in OWNERSHIP.md. If those are missing, collection refuses to run.
- Collection executes the whole guarded pool at each step (up to 64×3 calls per step) to form groups. This is the ranker's own data collection, not the frozen 6-pair episode budget, which the integration lane enforces at evaluation time.
- The pool comes from `bayes.generate(builtin plan, CaseRecord.candidate_seed)`. Missing coverage is computed from usable history tags. Both should be checked against the integration runner's state construction.
- Ranking metrics only. Episode metrics (pairs-to-witness, censoring) belong to the integration and measurement lanes.
