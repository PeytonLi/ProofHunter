# Pre-audit: integration bf4910b (not the final integrated SHA)

- Integration `bf4910b298c84930bb6b7243a5e7ecadf3444071` contains dataset c668241, ranker 9e6a812 and verification 945f9a8. `git diff 945f9a8 bf4910b -- research/verification tests/research/verification` is empty.
- Frozen paths: `git diff 280f28c bf4910b` is empty for `src bench schemas fixtures tools config .github`, root locks, `research/{schemas,spec,cli.py,pyproject.toml,uv.lock,OWNERSHIP.md,__init__.py}`.
- `pytest -q tests/research` on bf4910b: 348 passed, 11 skipped (134 s).

## Checkpoints (ranker real-v1 and fixture-v1)

- `sha256sum -c SHA256SUMS`: all OK for both sets.
- `checkpoint.json` `model_sha256` matches `model.txt` for all 8 checkpoints.
- Deterministic load (`lightgbm_determinism`, LightGBM 4.7.0): repeated loads give identical predictions for all 8.
- The `train_sha256` and `validation_sha256` in `checkpoint.json` (`103af9e3…`, `10ac81eb…`, `c2ea1b1f…`) do not match the published observation file hashes (`d349fb46…`, `1e88d2c4…`) or the regenerated ones (`82cf2429…`, `915f6515…`). Fixture checkpoints use `f712cb40…` / `7bd845ca…`. They are likely hashes of the ranker's derived matrices. I did not reproduce them, because independence rules keep me from reading the ranker code.

## Ledgers (baselines pilot and R1 validation; 946 entries)

| | Value |
|---|---|
| Measured USD | 3.3090 |
| Estimated USD | 0.3209 |
| Unknown-cost entries | 14 (meta, baselines pilot) |
| Paid providers | openai, meta. No Runpod, Fireworks or volume resources in the ledgers. |
| cost_ledger / teardown_ledger gates | PASS (with `job_cost_unknown` warnings) |

## Blocked for full reproduction

- Raw `RawResultRow` files are not committed. The pilot reports carry only `rows_sha256`. Metric recomputation, the budget audit and paired differences need the raw rows plus the reported-metrics JSON.
- There is no frozen test-split evaluation yet. The R1 reports are validation pilots only.
- The dataset still has the 3 normalised-diff test leaks (see PHASE2A.md). These are test clean controls only; train and validation are unaffected, so the ranker checkpoints are not affected.
- Live teardown listing has not been run, because provider keys are not set in the shell.
