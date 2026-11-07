# Measurement lane handoff (research-protocol-v1)

Branch `devin/1794090000-research/measurement`, rebased onto setup `cadc5b4d91828fec1adcb5341a11c2c466e57c1f`.
Schemas, interfaces and OWNERSHIP come from `13b96671`. No shared or production file was edited.

## Owned files

- `research/metrics/ledger.py`: `open_ledger(path) -> I.Ledger` keeps an append-only JSONL file of
  frozen `LedgerEntry` rows. It validates every row on write and on read, rejects duplicate ids and
  calls fsync after each entry. It also provides `make_entry`, `check_launch` (the 50 USD check-in
  guard), `overrun` (the 25 percent hard stop) and `totals`, keyed by measurement mode, category and
  phase. Unknown costs are counted but never added to dollar totals.
- `research/metrics/results.py`: writes and reads `RawResultRow` JSONL and a flat CSV. The label
  reader refuses the sealed test split unless it is explicitly unsealed. Also reads the protocol.
- `research/metrics/scoring.py`: the frozen `score`, `bootstrap_ci` and `paired_difference`.
  `paired_difference` takes an optional `labels=` keyword; the frozen positional signature still works.
- `research/metrics/teardown.py` and `research/metrics/commands.py`: `verify_teardown`.
- `research/metrics/synthetic.py`: schema-valid synthetic labels, rows and ledger for tests and demos.
- `research/reporting/plots.py` (SVG with no dependencies), `report.py` (`render_report`) and
  `commands.py` (`report`).
- `tests/research/metrics/` and `tests/research/reporting/`.

## What `score` reports

Results are grouped by mode, then split, then arm.

- **Recall at budgets 1, 3 and 6.** A run counts only if a `confirmed_*` verdict matches the label
  within k pairs. Recall is the mean over seeds for each case. Intervals are 95 percent percentile
  bootstrap with 10,000 resamples and seed 20261107, grouped by `defect_family`. Case-level
  intervals are reported too.
- **Recall by family.**
- **Precision** of `confirmed_*` verdicts.
- **Clean-control false positives:** run count and rate, with a confidence interval.
- **Inconclusive and failed runs:** overall rate and counts by `verdict:reason_code`.
- **Generator-fallback runs:** reason codes that contain `fallback`.
- **Confirmation rate, unusable probes and retries.**
- **Censored defect runs**, censored at 6. The median number of pairs counts detected runs only.
- **Invocations:** base, head, confirm and failed.
- **Timing:** p50 and p95 wall time, time to first witness (detected runs only), cold vs warm p50
  and mean timing for each component.
- **Tokens and cost:** token totals, known cost (with the estimated part and its run count labelled), runs with unknown cost, and dollars per case and
  per confirmed defect. The dollar ratios are `None` when any cost is unknown.
- **Degraded episodes:** per arm, the degraded run count and rate, with each reason's count and
  rate, plus the cost and confirmed defects of degraded runs. Degraded episodes stay in their
  arm's results (outcome and cost). `RawResultRow` has no `degraded` field (extra=forbid), so the
  records come from a sidecar JSONL `{"run_id": ..., "degraded": reason|null}` passed with
  `research report --degraded PATH` (`read_degraded` in `results.py`). Unknown run_ids are rejected.
- **Replicates:** recall for each seed, and the spread across seeds.
- **Paired contrasts** on identical (case, seed, repetition) episodes.
- **Factorial main effects and interaction**, when all four Qwen cells exist.
- **Curves:** recall vs executions (k = 0 to 6), vs dollars per episode and vs wall time per episode.

## Commands and results

```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research
  -> 98 passed, 2 skipped (the setup CLI-stub checks skip because report and verify-teardown now exist)
UV_OFFLINE=1 uv run --project research --locked mypy --strict research tests/research   -> no issues (23 files)
UV_OFFLINE=1 uv run --project research --locked ruff check research tests/research      -> all passed
UV_OFFLINE=1 uv run --project research --locked python -m research.schemas.export --check -> exit 0
Production (UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ...):
  pytest -q -> 1262 passed, 4 skipped (the numpy tests skip there)
  ruff check . -> clean; mypy src bench tools tests/integration -> clean (81 files)
  tools/gate.py setup -> PASSED; tools/gate.py integrated -> PASSED

# synthetic demo (D=research/artifacts/measurement/synthetic-demo)
uv run --project research --locked python -m research.metrics.synthetic --out $D/inputs
uv run --project research --locked python -m research.cli report --results $D/inputs/results.jsonl \
  --labels $D/inputs/labels-validation.json --ledger $D/inputs/ledger.jsonl --degraded $D/inputs/degraded.jsonl --out $D/report
uv run --project research --locked python -m research.cli verify-teardown --ledger $D/inputs/ledger.jsonl  -> ok
```

## Artifact hashes

These are the synthetic demo inputs. They are deterministic and git-ignored.

| file | sha256 |
|---|---|
| inputs/results.jsonl | 05fa03bdb72efbc8320f4408386fb03697878c3bb0e427bb9fe484777d90ed8d |
| inputs/labels-validation.json | d66e2cb9b7a943bc53de24e741dd17f513458d01c61abeb7e302639893d0cef9 |
| inputs/ledger.jsonl | 512c56fb83fd37f4b3b9be3089ac4f7c45469a0f6aaa72425ede111eec8d4d1a |
| inputs/degraded.jsonl | 87b11fc0a5a0905b50ea5a1c04d8f1d078d21a156a953bd4e67bb9c8974cbb51 |

This lane produced no real dataset or model artifacts.

## Costs

Zero paid calls, so the ledger has no real spend. No dataset or target-repository code ran.

## Known limitations

- Rows carry only episode totals, so recall vs dollars and recall vs time use the whole episode's
  cost and wall time. These equal the cost and time to the first witness only if the integration
  runner stops the episode at the confirmed witness. Otherwise the curves are conservative.
- The frozen schema has no `pairs_to_first_confirmed_witness` for `confirmed_existing_defect`.
  For that verdict, the step comes from the first `base_mismatch` probe.
- There is no dedicated field for generator fallback, so it is detected from a `reason_code`
  that contains `fallback`.
- Clean controls have no family, so each one is its own bootstrap cluster.
- numpy is in the research lock only. Bootstrap imports it lazily, and the scoring and report tests
  skip in the production environment. The plots are hand-written SVG because no plotting library
  is locked.
- Only synthetic rows were tested; no real episode rows exist yet.

## Remaining dependencies

- Baselines/integration lanes: write one degradation record per episode (`run_id`, `degraded`)
  next to the raw results. If `degraded` should live on `RawResultRow`, that is a setup-owned schema change.

- Integration lane: real `RawResultRow` output, ideally with confirmation that episodes stop at
  the witness.
- Dataset lane: the `LabelFile`s. Only the verification lane should pass `--unseal-test-labels`.
- Every paid lane: append `LedgerEntry` rows through `open_ledger` / `make_entry` so the report
  and `verify-teardown` can see them.
