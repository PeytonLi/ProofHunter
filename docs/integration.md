# Integration: offline wiring and release blockers

Integration started at `70826d59957b5e65d10afdf6f2c6c32996647b21`, verified
with `git log -1 --format=%H` after checking the repository was not shallow.
No live provider, deployment, webhook, Check Run or public ACP was used.

## Wiring and profiles

`pipeline.VerificationPipeline` injects SourcePort, MetaPort, RoutePort,
RunnerPort, CheckPort, RunStore and Clock. `compose` is the explicit profile
boundary: offline uses FixtureSource/Meta/Route/Checks with the production
`create_runner_port(settings)` (ReviewedFixtureRunner); live constructs the
GitHub, Meta, Snap and runner factories from validated Settings. Invalid live
settings fail closed. The CLI refuses live in this offline build; the live
composition was not executed.

All inference and evidence operations in the pipeline are production
`proofhunter.bayes` and `proofhunter.oracle`, not fixtures.reference. Each
candidate is evaluated before execution and every assessment uses
ProvenanceBinding.for_run. An unavailable oracle prevents even runner preflight.
Attestation precedes observations; denied isolation yields a partial receipt.
The selector excludes used hashes, confirmations get fresh attempt IDs, and
only the original usable pair contributes one Bayesian update. A reproduced
confirmation ends probing. Budgets are checked before runner spending, including
the reserved confirmation. Meta reserves up to two attempts without an outer
retry; Snap's requests_spent(run_id), when present, accounts for HTTP retries.
Async I/O is bounded by the run deadline. Smaller candidate limits truncate
the deterministic bank and recompute its hash.

Workers claim a 240-second lease (longer than the 180-second work deadline),
checkpoint fetching/planning/testing/finalizing, record charged attempts before
each call and completed observations after it, commit each complete probe, and
call finish once to atomically store the seal and terminal state. Store finish
owns the final completed/failed transition. LeaseLostError stops publication;
adapter failures produce partial inconclusive receipts where possible, while
unrecoverable internal failures produce sealed failed receipts with only
internal_error, never exception text. Partial receipts include unpaired attempts.
Offline never creates a check intent. Live check intents require both live mode
and github.enable_checks, and are not published by this worker.

Replay jobs use RunRecord.replay's input and exact source/commit bindings, carry
the parent receipt hash, and bypass Meta and Snap. The CLI independently verifies
raw receipt bytes before validate_replay, retrieves reviewed source bytes through
FixtureSource by bound commits and hashes, attests, evaluates the oracle and runs
fresh base/head/confirmation. It checks every replay observation's binding and
the observed values against the parent. Tamper, unavailable artifacts, mode
mismatch or non-reproduction are nonzero exits.

### Offline seed-17 bank decision

Use the PRD section 4 pinned offline candidate bank
`fixtures/demo/candidate_bank.seed17.json` only in offline composition at seed 17.
Still call production generate, but replace that profile's experiment bank with
the reviewed pinned bank. Other seeds and live use production generation. No
fixture was edited. This is the PRD's disclosed development bank, not a holdout
result or a changed selector. Inspection showed production generation also
selects the sentinel first and gives fixed full coverage, but its nonanchor
candidates/pool hashes differ. The pinned bank makes golden experiment histories
and their hashes directly comparable while preserving the exact production
selector, updater, oracle and classifier.

The buggy sentinel is `[15,15,15] @ 10%`: expected/base/head/repeated head =
`41/41/42/42`, confirmed_regression. Fixed is no_defect_found with full required
coverage; half_even is confirmed_regression; existing is confirmed_existing_defect.

Golden comparison covers both committed golden files (only buggy and fixed
exist). Normalization ignores IDs, timestamps including deadline_at, elapsed
durations, seals and replay commands containing IDs. Histories, candidate pool
hashes, vectors, selections, values, budgets and verdicts match. The sole
remaining difference is the runner transport: ReviewedFixtureRunner reports
stdout_sha256/stderr_sha256 as null, whereas FixtureRunner used to generate
goldens hashes its synthetic output and empty stderr. Tests assert equality
after **only** that explicit difference; they do not discard probe histories.
Repeated normalized production receipts are equal.

## Recovery: explicit fail-closed, not transparent resume

The frozen ports do not durably expose a pre-probe pinned revision or pre-call
provider reservations. RunEvent.detail is sanitized text, not a typed durable
state channel; AttemptRecord is for sandbox invocations, not provider requests.
Do not encode hidden pin/budget payloads into those fields.

Every recovered claim (epoch > 1), including runs already beyond fetching, is
terminated **without re-pinning** or any Source/Meta/Snap/runner call. It produces
inconclusive / revision_unavailable; Meta/Snap budgets are conservatively fully
spent. Committed probes can supply the original evidence bindings, but do not
authorize continuing execution. If sealing fails the worker fails the job with
sanitized internal_error. Tests simulate checkpoint to planning, crash, expired
lease, re-claim and assert zero external calls and terminal inconclusive.
This is an acceptable explicit fail-closed safety policy, **not** a transparent
recovery implementation. The missing contract blocks resumable verification,
not the safety of the offline integration.

## Cross-owner defects (not waived as future live work)

| Owner | Defect and evidence | Required owning-lane fix |
| --- | --- | --- |
| store (`src/proofhunter/api.py`, ready) + setup (ports/contracts) | `/readyz` validates DB/config then returns HTTP 200 `{"status":"ready","worker_heartbeat":"unavailable"}` with no worker ever started. This contradicts PRD section 6 readiness = schema AND heartbeat AND required mode config. `test_readyz_current_contract_defect` asserts the current behavior explicitly; it is evidence of a defect, not an acceptance assertion. A composition wrapper would not fix create_app's authoritative contract. | Setup: define `WorkerHeartbeat(worker_id: SafeToken, profile: Profile, config_fingerprint: Sha256Hex, observed_at: UtcTimestamp, expires_at: UtcTimestamp)` and `HeartbeatStore.write_heartbeat(heartbeat) -> None`, `read_heartbeat(profile, config_fingerprint) -> WorkerHeartbeat | None`, exposed via ApiDependencies and worker dependencies; writes use trusted store time, bounded TTL, monotonic updates. Worker writes at startup, during polling and while working (interval shorter than TTL). Store: persist/read heartbeat transactionally; readyz must return 503 when absent/expired/mode or config mismatched, and 200 only if all three readiness predicates pass. Update schemas/fixtures/ports centrally. |
| setup (contracts/ports) + store (durability) | RunRecord cannot recover pin/provider allocations before a committed probe. Re-pin risks accepting a moving head; retries risk exceeding caps. | Add `RunCheckpoint(pinned_revision: PinnedRevision | None, provider_allocations: {meta: NonNegInt, snap: NonNegInt})`, `RunStore.reserve_progress(lease: RunLease, checkpoint: RunCheckpoint) -> RunCheckpoint`, exposed on RunRecord. CAS-fence owner/epoch/expiry; pin immutable once set; allocations monotonic, capped by BudgetLimits and persisted before external calls, with atomic accounting. Reclaims return the original pin and reserved counts. Until then use the explicit fail-closed policy above. |
| bench (`bench/arms.py`, `bench/run.py`) | Hard imports/calls to fixtures.reference initialize/select/evaluate/classify_pair/confirmation_status/update/classify/compute_coverage/generate with no production injection point. The sample-benchmark G2 step therefore measures the setup reference copy, **not shipped code**. | Replace reference calls with proofhunter.bayes/proofhunter.oracle production functions, retaining only hash-allowlisted first-party fixture transport labeled offline_fixture. Do not monkeypatch from integration. Re-freeze evaluation policy/results and run independently reviewed holdout. |

The readiness defect is a genuine control-plane correctness blocker, even in
offline mode. The benchmark defect blocks claiming shipped-policy benchmark
evidence. G2's existing sample benchmark command can pass without resolving
that claim. No cross-owner path was edited; these require owning-lane changes.

## Nine-lane handoff and scope audit

Ran `git diff --name-only 5c3aecd0ec98eb7ad1a91c43d85bf940d52d67cf..70826d5`:
91 changed paths, grouped below. Every path belongs to its lane, including each
`docs/agent-handoffs/<owner>.md`; no setup/frozen path changed in the joined delta.
Read all nine handoffs, docs/setup.md, PRD lifecycle/limits, selection, evidence,
release gates and commands, tools/gate.py and fixtures/simulate.py.

| Owner | Joined path groups (including handoff) | Count | Boundary audit / pending |
| --- | --- | ---: | --- |
| github | src/proofhunter/github/, tests/github/, handoff | 8 | Source/check/validator/webhook factories; real App permissions, stale event accessor/head policy and check reconcile smoke pending. |
| meta | src/proofhunter/meta/, tests/meta/, handoff | 4 | attempts charged including errors, labeled built-in fallback; real schema/entitlement/data-handling gate pending. |
| snap | src/proofhunter/snap/, tests/snap/, handoff | 3 | requests_spent used for retries; real visibility/choice/pool/latency gate pending. |
| bayes | src/proofhunter/bayes/, tests/bayes/, handoff | 3 | Production initialize/generate/select/update wired; pinned offline bank decision disclosed above. |
| runner | src/proofhunter/runner/, tests/runner/, ops/sandbox/, handoff | 9 | Production offline runner; live account isolation/resources/deletion and tagged sweeper pending. |
| oracle | src/proofhunter/oracle/, tests/oracle/, handoff | 10 | Production binding, assessment, coverage/classification, sealing and replay wired. |
| acp | src/proofhunter/acp/, tests/acp/, handoff | 14 | Private API boundary; fresh ASI:One/mailbox/discoverability gate pending. |
| store | src/proofhunter/store/, src/proofhunter/api.py, tests/store/, handoff | 4 | SQLite intake/lease/attempt/probe/finish used; readiness and durable recovery defects above. |
| bench | bench/, tests/bench/, handoff | 36 | Sample output is reference-policy only; production-policy fix and fresh verifier's full independent holdout pending. |

No pre-commit configuration exists in the checkout. The locked TestClient stack
emits the pre-existing `StarletteDeprecationWarning: Using httpx with
starlette.testclient is deprecated; install httpx2 instead.` The store handoff
already diagnoses this pinned stack; the correct repository .venv is loaded.
No lockfile/dependency change or warning suppression was made.

## Commands and operational boundaries

All checks and commands use `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline` and
`uv run --locked`. README contains the exact section 9 commands. `serve` requires
explicit distinct test-only transport tokens; it never reads live tokens through
offline settings. API and worker share PROOFHUNTER_DB_PATH and bind privately by
default. Standalone demos otherwise use a temporary database. Examples under
ops/deploy are uninstalled systemd/env examples with every live flag false and
no secrets. ACP is an operator console example, not an enabled public service.

`python tools/gate.py integrated --evidence ~/ph-evidence` retains full subprocess
logs, both receipts, tampered refusal input and benchmark outputs. The top-level
log is saved separately as `~/ph-evidence/gate-integrated.log`. These receipts
are offline_fixture only. Nothing was deployed or visually verified; this is a
CLI/API integration, not an end-user UI validation. Remaining operator gates:
live providers/GitHub/Daytona isolation, stale-head publication and outbox drain,
public ACP, reviewed retention (7-day operational/30-day public receipts), tagged
cleanup, heartbeat and independent holdout. Do not claim those gates passed.

### Recorded offline verification

```text
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q
769 passed, 1 warning in 11.72s

UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check .
All checks passed!

UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration
Success: no issues found in 70 source files

UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline python tools/gate.py integrated --evidence ~/ph-evidence
[gate] sample bench rows: 120
[gate] integrated PASSED
```

The integrated gate additionally reports mypy clean over src/bench/tools (69
files) and integration tests (1 file), fixture/schema snapshots (107 files),
contract tests (205 passed), both sealed demos, successful counterexample replay
and expected tamper refusal. Passing this executable gate does not resolve the
readiness or reference-only benchmark defects documented above.
