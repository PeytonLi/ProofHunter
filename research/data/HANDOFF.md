# Dataset lane handoff

- Setup SHA: `cadc5b4d91828fec1adcb5341a11c2c466e57c1f` (schemas/interfaces at `13b96671`).
- Branch: `devin/1794090000-research/dataset`. Artifacts were generated from code at `3cbb199`
  (template code is unchanged after that commit; later commits only touch lint/docs).
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
| test | 500 | 139 (target 150) | 6 | 58 |

- Manifest `cases_sha256` `ba3d927d994c4c54944ab42a03b2b253ac97bbfa0193fca37e28b22c87b21520`.
- Every defect: base or reference equals the oracle on a deterministic probe set, head violates it,
  and fresh re-execution reproduces the witness. The test split (`labels/test.sealed.json`) is only
  readable through `store.labels('test', unseal='research-protocol-v1:<sha256 of file>')`.
- Splits are grouped by shape, family, defect origin, near-dup cluster and template. The audit
  (`reports/track_a/split-audit.json`) reports 0 cross-split keys, 0 shared source hashes, 0 shared
  behavior fingerprints and 0 overlap with the production benchmark.
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
| case-manifest.json | 1f626d3eb695a80992586ea168bb26fac7cce3c7b5dd576810e73c4ee99590e0 |
| labels/train.json | 3905f2041c4726d0054b54685bbd10446bb9a8266bf037fe9309cb922d49c395 |
| labels/validation.json | bddd8d213dc6958f8a4282b2164a8a34ac0908be855edda960491519c71546cc |
| labels/test.sealed.json | f9033e8a7be8bdd1aea06ff1e5a75842d214d1a7888ff0c9426c04c1c8dce676 |
| observations/train.jsonl (32,129 rows) | d349fb46b948813ad19fc26dca119904d507f7b70cd5c9cfc272c11e9dd83d2c |
| observations/validation.jsonl (4,282) | 1e88d2c4573eb2bac1e8b8197e36b97f0be798afdd90e6b2260a71b55e289cb7 |
| generator/train.jsonl (2,000) | cc5c14121f548500dff1aa3a2810dea02e33b737b901d1b3d22debe2ad5e48e9 |
| generator/validation.jsonl (300) | 57a119d8bbf92b9816e0af52bb40fd597808818a144dc896b4593e6910c48866 |
| public tarball (no sealed labels) | 046c59ca78329a9f31ea98d196bf95cd24571cc69f38b030851e5b47b151da98 |

## Track B (metadata only, nothing executed)

- Dataset: `SWE-bench/SWE-smith-py@77cab9055d42ab4a5c25c89a8f937096db13558e`, 50,908 rows. The 11
  Parquet shard hashes are listed in `reports/track_b/exclusion-report.json`.
- Tasks: train 2,714 (79 repositories), validation 275 (17), test 491 (30), with no repository in
  more than one split.
- Candidate tests: FAIL_TO_PASS, then PASS_TO_PASS, capped at 200 allowlisted IDs. They must be run
  from the clean mirror commit (`repo_commit`).
- Output hashes: train `7d47ac2a…09d5`, validation `528a50c6…7b5`, test `21f9c16d…7268`.

## Tests

- `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research`
  gave 82 passed, 2 skipped.
- `uv run --locked pytest -q` (production) gave 1259 passed, 2 skipped.
- `git diff origin/devin/1794090000-research/setup -- src bench schemas fixtures tools config pyproject.toml uv.lock .github research/schemas research/spec research/cli.py`
  is empty.

## Known limitations

- Clean controls are short in train (333 of 400) and test (139 of 150). Variants whose source text
  collided across splits were removed (`cross_split_source_duplicate`).
- Track B:
  - Rows carry `dataset_id="SWE-bench/SWE-smith"` because the frozen schema forces that value; the
    true dataset is SWE-smith-py. Setup needs to widen the schema.
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
- Setup lane: widen the Track B `dataset_id` value if SWE-smith-py rows must validate under its
  real ID.
