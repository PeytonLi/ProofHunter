# Oracle and evidence handoff

- Role: oracle. Branch `devin/1794078900-proofhunter/oracle`, started from the G0 freeze `5c3aecd0ec98eb7ad1a91c43d85bf940d52d67cf`.
- Profile: `PROOFHUNTER_PROFILE=offline`. No live keys were read, no provider service was called, no PR code was run outside the reviewed fixture revisions.
- Owned paths changed: `src/proofhunter/oracle/`, `tests/oracle/`, this file. No setup paths, fixtures, schemas or `uv.lock` changed.

## What is implemented

`proofhunter.oracle` exports the six required callables plus helpers for the pipeline.

| Export | Port | Behavior |
|---|---|---|
| `evaluate(enrollment, candidate)` | `Evaluator` | T1 `checkout-rounding@1` oracle: `n = sum(prices) * (100 - discount)`, `(n + 50) // 100`, pure integers. It looks up reviewed oracles by `(contract_id, oracle_version)` in `TRUSTED_ORACLES`. A disabled enrollment or an unknown oracle version gives `status=unavailable`, tier T3, no expected value. Booleans, floats, out-of-range values or an input hash that does not match the values give `status=invalid`. Independent of the Bayes predictors and of `fixtures.reference`. |
| `classify(probes, coverage, stop_reason)` | `Classifier` | `verdict-rules-v1`. It recomputes every probe from its oracle and observations and never trusts stored labels. Order: record integrity and cross-probe provenance (`trust_failure`), reproduced confirmation (`confirmed_regression` / `confirmed_existing_defect`, takes precedence), replay disagreement (`nondeterministic`), changed pre-existing mismatch, oracle unavailable, trust failure exclusion, `denied` execution (`isolation_unverified`), blocking stop reasons, clean coverage (`no_defect_found` needs all probes usable matches, T1 oracle, all required tags and at least 3 distinct pairs), then insufficient evidence. The supplied `coverage` must equal the recomputed coverage. |
| `compute_coverage(probes, coverage_guard_used)` | `CoverageFn` | Required tags observed only on recomputed exact base = head = expected pairs, distinct by input hash. |
| `seal(receipt)` | `Sealer` | Re-validates the receipt through JSON (catches `model_construct` bypasses, `internal_error`), checks the receipt agrees with its own evidence (`trust_failure`), then sets `receipt_sha256` with the shared `compute_receipt_sha256`. Reproduces the golden receipt hashes exactly. |
| `verify_receipt(receipt_json)` | `ReceiptVerifier` | Strict parse: UTF-8, at most 1 MiB, JSON object, no duplicate keys, no NaN/Infinity. The stored hash must match the raw body, the normalized model and `verify_receipt_sha256`, otherwise `receipt_tampered`. A correctly hashed receipt that contradicts its evidence raises `trust_failure`. |
| `validate_replay(receipt, probe_no)` | `ReplayValidator` | The receipt must be sealed and intact (`receipt_tampered`) and semantically consistent. `None` selects the counterexample. The stored replay is returned after binding checks: input values and hash, pinned SHAs, source hashes, harness, image, snapshot, contract and oracle version, execution mode, and a command equal to `build_replay_command` for this run ID. Any other in-range probe gets a replay derived from the receipt with `--probe N`. Out of range or no counterexample gives `replay_unavailable`; a binding mismatch gives `trust_failure`. |

Pipeline helpers: `assess_probe(oracle, base, head, confirmation, binding=...)` returns `PairAssessment` (`evidence_kind`, `exclusion_reason`, `confirmation_status`, the Bayes `outcome` or `None`, and `needs_confirmation`). `ProvenanceBinding.for_run(...)` binds the requested commits, source hashes, enrollment snapshot/image/harness and runner mode. `classify_pair`, `confirmation_status`, `exact_value` (rejects non-`ok`, truncated, bool, non-int, out-of-int64 values), `build_replay(...)`, `replay_binding(replay)` and `check_replay_observation(replay, observation, side)` validate a fresh replay run's observations. `audit_probes` and `check_receipt_semantics` are the shared integrity checks.

## Interface assumptions for integration and store

- The pipeline calls `evaluate` before invoking the runner and never puts the result in an `ExecutionRequest`.
- The pipeline should call `assess_probe` with a `ProvenanceBinding` built from the pinned revision, the verified `SourceBundle` hashes and the enrollment. The stored `ProbeRecord` labels must equal the returned assessment, or `classify` and `seal` report `trust_failure`.
- A confirmation is attempted only when `needs_confirmation` is true. It must use a fresh attempt ID, `side=confirm`, and the same head commit, source, input, snapshot, image and harness. It never adds a second posterior update.
- A repeated input may be stored only as `evidence_kind=excluded` with `exclusion_reason=duplicate`; it does not count toward coverage.
- Stop reasons: `budget_exhausted`, `inputs_exhausted`, `coverage_complete`, `confirmed_evidence` are ordinary. Anything in `BLOCKING_STOP_REASONS` gives `inconclusive` with that reason. `confirmed_evidence` without a reproduced chain gives `trust_failure`.
- Receipts with probes need `pinned_revision` and `source_file_sha256`. A live receipt's observations must have `runner_mode=daytona`; offline ones `offline_fixture`.
- `seal` raises `AdapterError` rather than sealing an inconsistent receipt. Integration should map that to a failed job or an `inconclusive` partial receipt.
- CLI `replay` should call `verify_receipt` on the file bytes first, then `validate_replay(receipt, None)` for `--probe counterexample` or the integer probe number. A nonzero exit on any `AdapterError` covers the tamper, missing artifact and mode mismatch requirements. Fresh replay observations can be checked with `check_replay_observation` and assessed with `assess_probe(..., binding=replay_binding(replay))`.

## Intentional differences from `fixtures.reference`

- `denied` exclusions map to `isolation_unverified` instead of falling through to `coverage_insufficient`.
- Trust failures (spoofed provenance, forged labels, mismatched coverage, mixed sources between probes, reused attempt IDs) give `inconclusive` with `trust_failure`.
- Stricter receipt parsing and semantic checks on `seal`, `verify_receipt` and `validate_replay`.
- On every fixture scenario without these conditions, the verdict, reason and counterexample probe match the reference. A test checks this.

## Tests (tests/oracle, 103 tests)

They cover: the 41/41/42/42 chain, the half-even mutant, an unconfirmed head mismatch, a confirmed pre-existing defect and a changed pre-existing mismatch, a flaky confirmation (43) and an unusable one (timeout), the oracle unavailable both before execution and per probe, clean coverage on the fixed case, partial coverage, forged coverage, blocking stop reasons, unusable probes, and hostile output. The hostile-output cases are bool, float, string and oversize values, a missing value, truncated output, spoofed source/commit/harness/input/image/snapshot, a head posing as base, a reused attempt as confirmation, a forged evidence label, mixed head sources and denied execution. Receipt tests cover the golden receipts verifying and resealing to the same hash, nine tamper mutations, malformed bytes, duplicate keys, NaN, a re-hashed forgery, an unsupported clean claim, an unvalidated receipt, counterexample and derived replays, replay provenance and command forgeries, `build_replay` equal to the simulated replay, fresh replay observation binding, port conformance, and that no fixtures are imported from production code.

Fixtures used: `fixtures.catalog`, `fixtures.simulate.simulate_case`, `fixtures.adapters.FixtureRunner` (fault injection), `fixtures.reference` (cross-check only, in tests) and `fixtures/demo/receipts/*.seed17.json`.

## Commands and results

```text
uv sync --locked                                                        ok
UV_OFFLINE=1 uv run --locked ruff check src/proofhunter/oracle tests/oracle   All checks passed
UV_OFFLINE=1 uv run --locked mypy src/proofhunter/oracle tests/oracle         no issues (9 files, strict)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q tests/oracle   103 passed
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q             308 passed
UV_OFFLINE=1 python3 tools/gate.py setup                                PASSED
```

There are no pre-commit hooks configured in the repo. `tools/gate.py integrated` was not run because it needs the other roles' modules.

## Known blockers and setup notes

- None blocking. The frozen contracts were enough.
- Setup note, not a defect: `OracleResult` needs a `trust_tier` even when no oracle exists. Unavailable results use T3, so they can never satisfy the T1 gates.
