# Phase 2a dataset verification

- Dataset branch `devin/1794090000-research/dataset` at `ac50b9838ed359f8923961bca8c38ac0c69ca0c0`, on setup `280f28cc0cca2449179501e0e0073cd63e617a82`
- Verifier: `devin/1794090000-research/verification` at `a0587789778f06c21f261d05e1e519aeabcc40b2`
- Public tarball sha256: `88dc28f10b1a6357880d02f972b01ace55cc1edc46e7dc29d39f944e4cdce308`. The b48a61f tarball was `046c59ca…da98`.
- Commands:
  - `uv run --project research --locked python -m research.verification <tarball-root|regen-root> --out <dir>`
  - `python -m research.cli prepare-splits --out ~/phase2a/regen2`, run in an ac50b98 worktree (wall time about 4 minutes)
- Only reviewed Track A template code was executed. No Track B, dataset or target code was run. Cost: $0.

## Track A equality (ac50b98 vs b48a61f)

- **Tarball:** every Track A file is byte-identical to the b48a61f tarball. Only the Track B files and reports differ.
- **Manifest:** `cases_sha256` is `ba3d927d994c4c54944ab42a03b2b253ac97bbfa0193fca37e28b22c87b21520`, with 3,332 cases.
- **Regeneration at ac50b98:**
  - `cases` are identical. The only metadata difference is `dataset_commit` (tarball cadc5b4, regenerated ac50b98).
  - These files are byte-identical: labels train/validation, generator train/validation.
  - Observations (32,129 train, 4,282 validation) are identical except for the `cost.wall_ms` timing field.
  - Canonical observation hashes without `wall_ms`: train `9771b310…69d8`, validation `75b42b72…0d1a`.
  - Sealed test labels from the regeneration: `f9033e8a7be8bdd1aea06ff1e5a75842d214d1a7888ff0c9426c04c1c8dce676`. They were used only for the provenance audit.

## Track B

- `dataset_id` is `SWE-bench/SWE-smith-py`, at revision `77cab9055d42ab4a5c25c89a8f937096db13558e`.
- Manifest hash `3b8e1086acd54e2678061bca3dd5225452790f465d61705e47c42d9f61412433`, independently recomputed: matches.
- Rows: 2,714 train, 275 validation, 491 test. Repositories: 79 / 17 / 30, with no repository shared across splits.
- `executed=false`. The `SweSmithTask` schema check and `split_leakage_track_b` both PASS.

## Gates

| Gate | Tarball | Regen (sealed labels) |
|---|---|---|
| production_frozen, schemas_frozen, protocol/spend schemas | PASS | PASS |
| schema: CaseManifest, LabelFile, RankingObservation, GeneratorExample | PASS | PASS |
| schema: SweSmithTask | PASS | n/a |
| source_hashes (content-addressed `sources/<sha256>.py`) | PASS | PASS |
| **split_leakage (Track A)** | **FAIL** | **FAIL** |
| label_provenance (independent oracle and production oracle) | PASS (warning: sealed test labels not in tarball) | PASS, all 3,332 labelled |
| test_label_unreachable (no test ids/hashes in train/val artifacts) | PASS | PASS |
| observations / generator: split and provenance | PASS | PASS |
| split_leakage_track_b | PASS | n/a |
| cost_ledger, teardown_ledger (no paid resources) | PASS | PASS |

## Discrepancies

1. **Cross-split near-duplicate changes (error).** There are 7 pairs, covering 13 clean controls, where the base→head changed lines are token-identical across splits (Jaccard 1.0).
   - Train `clean_s06` (4 cases) matches test `clean_s10` (4).
   - Validation `clean_s09` (3) matches test `clean_s11` (2).
   - Example: test `a-0ba3f8d684ce1f42c2df` and validation `a-71a0d2927d6688c546f6` share the same 15-line refactor diff (`_round_half_up`→`_round_cents`, while→for).
   - The whole-file hashes, `family_id`, `template_id` and `near_dup_cluster` all differ, so the dataset lane's own split audit (which groups by those keys plus behaviour fingerprint) reports 0.
   - Impact: a ranker or generator that sees the train/validation diff can memorise these test clean controls. It is limited to clean controls, and no defect case is affected.
   - Suggested fix: cluster clean templates by normalised diff body, or drop or regroup those 13 cases.
2. **Below frozen `split_targets` (warning).** Clean controls are train 333 < 400 and test 139 < 150 (validation 60 meets its target of 60). Defect counts meet their targets.
3. **Tarball omits `labels/test.sealed.json`.** This is by design. Full label provenance was verified on the deterministic regeneration instead.

## Addendum: normalised-diff check

`split_leakage` now also compares base→head changes after normalisation (`normalised_change_text`):
- Comments, docstrings, blank lines and layout are dropped.
- Strings collapse to `S`.
- Non-builtin identifiers are alpha-renamed by first occurrence, using one renaming shared by base and head.
- Number literals are kept.
- Pairs across splits with Jaccard ≥ 0.9 on 5-token shingles, or an exact match, are errors (`near_duplicate_normalised_change`).
- Edits that change only comments or whitespace normalise to an empty change and are ignored.

Baseline on ac50b98 (regenerated): no additional pairs beyond the 7 raw pairs (13 clean controls) already reported. The run took 36 s.

## Re-audit: dataset c668241 (`c668241b8a9bb71e375930d925cc7f829bd4f48e`, normal push on ac50b98)

- Verifier: `945f9a87d620614961adccffbbe02610248081b1`.
- The diff from ac50b98 touches only dataset-owned `research/data/**` and `tests/research/dataset/**`.
- Regenerated with `python -m research.cli prepare-splits --out ~/phase2a/regen3` in a c668241 worktree (3 min). Only reviewed Track A template code was run. No new tarball was supplied, so no tarball audit was done. Cost: $0.
- Manifest `cases_sha256` is `dcd14e3f8e17533d89dd526d1d801493bfe019c58939cdc806ff87e637459340`, with 3,343 cases. Sealed test labels: `2344b8a2897c191c78132e69cfc59a8e4cfa446f4d5db91155199f4b3a7524e0`, matching the claim.

### Train/validation preservation (independent comparison with the ac50b98 regeneration)

| Artifact | Result |
|---|---|
| Manifest entries, train 2,333 / validation 360 (order and content) | identical |
| Source files for train/validation (2,609 / 418, content-addressed) | identical |
| Label arrays, train/validation | identical; the files differ only in `manifest_cases_sha256` (expected re-binding) |
| `generator/train.jsonl`, `generator/validation.jsonl` | byte-identical (`cc5c1412…`, `57a119d8…`) |
| `observations/{train,validation}.jsonl` (32,129 / 4,282) | identical row-for-row except `cost.wall_ms`, which varies between regeneration runs. Byte-identity holds only for the published files (`d349fb46…`, `1e88d2c4…`) and cannot be reproduced by regenerating. |

### Test repair

- 40 test clean controls were dropped (`clean_s10`/`clean_s11`) and 51 added (`clean_s10`/`s11`/`s12`).
- The retained test entries are unchanged. Test is now 500 defect + 150 clean.
- None of the 13 previously flagged cases remain.

### Gates (regeneration with sealed labels)

| Gate | Result |
|---|---|
| production_frozen, schemas_frozen, protocol/spend, all artifact schemas, source_hashes | PASS |
| split_leakage: case/family/origin/cluster/template/head-hash grouping, benchmark overlap, raw-diff near-duplicates (0 pairs) | pass (sub-checks) |
| **split_leakage: normalised-diff near-duplicates** | **FAIL: 3 pairs** |
| split_leakage targets | warning: train clean 333 < 400 (test 150 and validation 60 now meet their targets) |
| label_provenance (independent and production oracle, all 3,343 labelled, including the sealed test labels) | PASS |
| test_label_unreachable, row_splits and provenance for observations and generator | PASS |
| cost_ledger, teardown_ledger | PASS |

### Remaining leaks (identifier-renamed and docstring-only rewrites of the same edit)

| Test case | Matches | Shared normalised edit |
|---|---|---|
| `a-ec3656b89a5898523ddf` (clean_s12) | validation `a-1ec6c295a9da6e2521e2` (clean_s09) | while-loop sum → `reduce(operator.add, …)`, plus imports |
| `a-361514a4ab44b37d7711` (clean_s12) | train `a-63451b5132493288b190` (clean_s01) | `sum(...)` → index while-loop |
| `a-710323f91a061f997429` (clean_s12) | train `a-7db40b3d9a06b65c5971` (clean_s01) | `sum(...)` → empty-guard + for-loop |

In each pair the raw diffs differ only in identifier names (`items/d/subtotal` vs `basket/off/amount`, helper `_round_half_up` vs `_half_up`) and in docstrings. The dataset lane's `normalized-diff-v1` does not merge these. Its stats report 3,286 fingerprints for 3,343 cases, so identifiers are probably not alpha-renamed. That is an inference: I did not read its code.

Suggested fix: drop or replace these 3 test cases (train and validation stay untouched). Making normalized-diff-v1 alpha-rename identifiers would prevent the problem from recurring.
