# Dataset lane handoff

- Setup SHA: `280f28cc0cca2449179501e0e0073cd63e617a82`, which widens `SweSmithTask.dataset_id`.
  It sits on `cadc5b4`, with schemas and interfaces at `13b96671`.
- Branch: `devin/1794090000-research/dataset`.
- Track A test split was repaired twice after verifier findings (PHASE2A on `ac50b98`: 13 test
  clean controls; re-audit of `c668241`: 3 more). Test clean controls whose normalised base->head
  edit matched a train/validation case were replaced. Train and validation were not rebuilt; see
  `reports/track_a/trainval-preservation.json` for before/after hashes against `ac50b98`.
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

- Manifest `cases_sha256` `897109b31604ebb62dfd5e8334dcf5a5c244527fc319eddbf573527f90fa7b94`.
- Every defect: base or reference equals the oracle on a deterministic probe set, head violates it,
  and fresh re-execution reproduces the witness. The test split (`labels/test.sealed.json`) is only
  readable through `store.labels('test', unseal='research-protocol-v1:<sha256 of file>')`.
- Splits are grouped by shape, family, defect origin, near-dup cluster, template and normalised
  diff (`normalized-diff-v2`, `splits.diff_signature`):
  - Comments, docstrings and whitespace are stripped and strings collapse to `STR`.
  - The changed lines are then hashed after renaming every non-builtin identifier to `v<n>` in
    order of first appearance, with one renaming shared by base and head.
  - A cross-split pair is rejected on an exact fingerprint match or a Jaccard similarity of at
    least 0.9 on 5-token shingles (`splits.DiffIndex`).
- The audit (`reports/track_a/split-audit.json`) reports 0 cross-split grouping keys, 0 exact
  diff fingerprint matches, 0 shingle-Jaccard pairs of at least 0.9, 0 shared source hashes, 0
  shared behavior fingerprints and 0 overlap with the production benchmark. v2 flags all 16 test
  cases the verifier reported, and no validation case matches train.
- Test repair (`build.repair_test_split`):
  - Test cases matching a train/validation diff are dropped: 65 clean controls from
    `clean__s10`/`clean__s11` of the original selection.
  - They are replaced by test-only clean variants (`build.enumerate_test_clean_backfill`) that
    also refactor the base's subtotal/numerator form, checked against the same index.
  - All 500 test defect cases are unchanged.
  - The 650 sealed test labels were re-checked against the oracle. All 150 clean controls agree
    on all 3,200 probes, and all 500 witnesses reproduce on fresh execution (442 introduced
    regressions with an oracle-correct base, 58 existing defects).
- `clean__s12` is not a generalisation of `s01`/`s09`: its rounding helper (`float`) differs.
  A clean diff never touches the helper body, only the subtotal/numerator lines and renames. The
  3 re-audit leaks were backfill variants whose base reused the `s01` (`sum`/`mul`) or `s09`
  (`while`/`commuted`) subtotal/numerator form, so the edit matched those templates' clean edits.
  Any test shape could do this, so the fix is the v2 diff filter rather than dropping `s12`.

| artifact | sha256 |
|---|---|
| case-manifest.json | c7b6072c80a2b060539c51a863d487006cc8da042ceca695903d10e093f38cf1 |
| labels/train.json | 63fa3261084b0b95f5c7049bf9e9a693819e6d97b6777ffe89d12f18dfb5ed97 (label array unchanged) |
| labels/validation.json | 683e0263494b6891b31e450b1a45c0c4c0f0377657c2f05fbd47218169d64c78 (label array unchanged) |
| labels/test.sealed.json | edbc940aa94044163d52e6da415f70b24c8131e7830f10ca56ed32bb8ad9059e |
| observations/train.jsonl (32,129 rows) | d349fb46b948813ad19fc26dca119904d507f7b70cd5c9cfc272c11e9dd83d2c |
| observations/validation.jsonl (4,282) | 1e88d2c4573eb2bac1e8b8197e36b97f0be798afdd90e6b2260a71b55e289cb7 |
| generator/train.jsonl (2,000) | cc5c14121f548500dff1aa3a2810dea02e33b737b901d1b3d22debe2ad5e48e9 |
| generator/validation.jsonl (300) | 57a119d8bbf92b9816e0af52bb40fd597808818a144dc896b4593e6910c48866 |
| public tarball (no sealed labels) | 6d33457c560f1c79a12c62af9665dbbb1583cf5d81e008333022c7dcec9d5f01 |

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
  gave 87 passed, 2 skipped (after the normalized-diff-v2 test-split repair).
- `uv run --locked pytest -q` (production) gave 1259 passed, 2 skipped at b48a61f; not re-run since, because the diff against setup for production and shared paths is empty.
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

## SFT hypothesis-type labels (diagnosis, no data changed)

`sft.MODE_BY_FAMILY` maps `per_line_rounding` to `per_item_rounding` and `half_even_rounding` to
`half_even_rounding`; every other family maps to `other`. The production `HypothesisMode` has only
those three values. The counts (train 1,958 other + 42 per_item + 0 half_even; validation 300
other) are the real distribution, not a mapping bug:
- `half_even_rounding` is a test-only family (41 test cases). The family-disjoint split puts it
  nowhere else, so train and validation have 0 half-even cases.
- `per_line_rounding` has 42 train cases. The other 12 train families and the 4 validation
  families are not per-item or half-even rounding.
- Behavioural check over the 3,200 probes: 1 train head matches `predict_per_item` exactly and
  none matches `predict_half_even`. Matching on the witness value alone gives 390 half-even and
  104 per-item, all coincidental, so it is not a valid labelling.
