# Dataset lane handoff

- Setup SHA: `280f28cc0cca2449179501e0e0073cd63e617a82`, which widens `SweSmithTask.dataset_id`.
  It sits on `cadc5b4`, with schemas and interfaces at `13b96671`.
- Branch: `devin/1794090000-research/dataset`.
- Track A test split was repaired after verifier PHASE2A (dataset `ac50b98`): 13 test clean
  controls had a base->head diff identical, after normalisation, to train/validation cleans.
  Train and validation selections were not rebuilt; see
  `reports/track_a/trainval-preservation.json` for before/after hashes.
- Track B was regenerated after the rebase onto `280f28c`, with
  `dataset_id="SWE-bench/SWE-smith-py"`.
- Owned files: `research/data/**`, `tests/research/dataset/**`. Nothing shared or production edited.

## Regenerate (deterministic, about 4 min, no network, no paid calls)

    UV_OFFLINE=1 uv run --project research --locked python -m research.cli prepare-splits \
      [--swe-smith-source <dir with metadata.jsonl, shards.json, pins.json, split_proposal.json>]

Track B `metadata.jsonl` comes from the pinned Parquet shards (data read only):
`uv run --project research --locked --with pyarrow==18.1.0 python -m research.data.swe_smith export <parquet_dir> <out>`.

## Track A (`checkout-rounding@1`)

| split | defect | clean | defect families | existing_defect |
|---|---|---|---|---|
| train | 2000 | 333 (target 400) | 14 | 205 |
| validation | 300 | 60 | 4 | 29 |
| test | 500 | 150 | 6 | 58 |

- Manifest `cases_sha256` `dcd14e3f8e17533d89dd526d1d801493bfe019c58939cdc806ff87e637459340`.
- Every defect: base or reference equals the oracle on a deterministic probe set, head violates it,
  and fresh re-execution reproduces the witness. The test split (`labels/test.sealed.json`) is only
  readable through `store.labels('test', unseal='research-protocol-v1:<sha256 of file>')`.
- Splits are grouped by shape, family, defect origin, near-dup cluster, template and normalised
  diff fingerprint (`normalized-diff-v1`, `splits.diff_fingerprint`): base and head lines are
  compared with whitespace and comments removed, then changed lines are hashed with strings as
  `STR`, template identifiers collapsed to their role and other names to `ID`. The audit
  (`reports/track_a/split-audit.json`) reports 0 cross-split keys (including diff fingerprints),
  0 shared source hashes, 0 shared behavior fingerprints and 0 overlap with the production
  benchmark.
- Test repair (`build.repair_test_split`): test cases whose diff fingerprint occurs in train or
  validation are dropped (40 clean controls from `clean__s10`/`clean__s11`, a superset of the 13
  the verifier flagged) and replaced by 51 test-only clean variants
  (`build.enumerate_test_clean_backfill`) that also refactor the base's subtotal/numerator form.
  All 500 test defect cases are unchanged. The 650 sealed test labels were re-checked against the
  oracle: 150 clean controls agree on all 3,200 probes, and all 500 witnesses reproduce on fresh
  execution (442 introduced regressions with an oracle-correct base, 58 existing defects).
- Ranking observations (train/validation only):
  - Policy `eps0.3-heuristic-v1`: 3 episodes per case, at most 6 pairs each, and at most 18 rows
    per case. Selection probabilities are recorded.
  - Features (`dataset-features-v1`) are computed before execution.
- Generator SFT examples (`gen-prompt-v1`):
  - One example per train/validation defect case.
  - Inputs are the contract, spec hash, interface, base/head files and the diff. Template params,
    ids, families and fix patches are excluded.

| artifact | sha256 |
|---|---|
| case-manifest.json | aaa8ea78fc2826b05bdeb5e434a7697461abd88a81980436019f387374b58a04 |
| labels/train.json | 7163a3af8adbef1d7f66e30e45ab26f6552a1207e6a44277fe04c5bb50e9c596 (label array unchanged) |
| labels/validation.json | 8f4124e64e6925bd15085eaec431b2154317a43417b5eb44c5a43f1763bea72a (label array unchanged) |
| labels/test.sealed.json | 2344b8a2897c191c78132e69cfc59a8e4cfa446f4d5db91155199f4b3a7524e0 |
| observations/train.jsonl (32,129 rows) | d349fb46b948813ad19fc26dca119904d507f7b70cd5c9cfc272c11e9dd83d2c |
| observations/validation.jsonl (4,282) | 1e88d2c4573eb2bac1e8b8197e36b97f0be798afdd90e6b2260a71b55e289cb7 |
| generator/train.jsonl (2,000) | cc5c14121f548500dff1aa3a2810dea02e33b737b901d1b3d22debe2ad5e48e9 |
| generator/validation.jsonl (300) | 57a119d8bbf92b9816e0af52bb40fd597808818a144dc896b4593e6910c48866 |
| public tarball (no sealed labels) | c5a159955b7ac807e0acc62ce0b8450be8a96d29d339d0afe671ab61a5b9defc |

## Track B (metadata only, nothing executed)

- Dataset: `SWE-bench/SWE-smith-py@77cab9055d42ab4a5c25c89a8f937096db13558e`, 50,908 rows. The 11
  Parquet shard hashes are listed in `reports/track_b/exclusion-report.json`.
- Tasks: train 2,714 (79 repositories), validation 275 (17), test 491 (30), with no repository in
  more than one split.
- Candidate tests: FAIL_TO_PASS, then PASS_TO_PASS, capped at 200 allowlisted IDs. They must be run
  from the clean mirror commit (`repo_commit`).
- Every row has `dataset_id="SWE-bench/SWE-smith-py"` and
  `dataset_revision="77cab9055d42ab4a5c25c89a8f937096db13558e"`.
- Output hashes:
  - train: `c48460a7c48563626ef8fa4838cb23239cd5f5c46ed9249d474f1a452b6d00a8`
  - validation: `ddd58ea5595e707c536d121c7e167e8710da85ec2673129d4c05f1f04ffdf65f`
  - test: `ec5b0783c1d7bf546e67e1873986d4e1b924348be2ca5124b1846b0b78af3377`
  - manifest hash (sha256 of train, validation and test concatenated in that order):
    `3b8e1086acd54e2678061bca3dd5225452790f465d61705e47c42d9f61412433`

## Tests

- `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research`
  gave 86 passed, 2 skipped (after the test-split repair).
- `uv run --locked pytest -q` (production) gave 1259 passed, 2 skipped.
- `git diff origin/devin/1794090000-research/setup -- src bench schemas fixtures tools config pyproject.toml uv.lock .github research/schemas research/spec research/cli.py`
  is empty.

## Known limitations

- Clean controls are short in train (333 of 400); train was frozen for the ranker and generator
  runs, so it was not backfilled. Validation (60) and test (150) meet their targets.
- `labels/train.json` and `labels/validation.json` changed bytes only in `manifest_cases_sha256`,
  which the frozen `LabelFile` schema ties to the manifest. Their label arrays, manifest records,
  source files, observations and SFT examples are byte-identical.
- Track B:
  - My filters reproduce the proposal approximately, not exactly: 130 proposal IDs are dropped.
  - `license_verified` means only that GitHub reported an SPDX ID. NOASSERTION repositories are
    marked unknown.
- Lint and type checks:
  - `ruff`: style-only findings remain in `templates.py`. I left them because editing that file
    would change the template hashes.
  - `mypy`: 32 implicit re-export errors from using the `models` namespace. These are type-check
    errors only and do not affect runtime.
- The observation policy is a heuristic epsilon-greedy policy, not the production Bayesian selector.
  Clean and base-mismatch rows have relevance zero or unknown.

## Costs and dependencies

- Cost: $0. No paid calls, no provisioned resources, no secrets used. Compute was local CPU only.
- Ranker and generator lanes can consume the artifacts above.
