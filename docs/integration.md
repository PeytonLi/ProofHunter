# Integration release: offline G2

Starting SHA: `77c8062b17527c22d22d19d786a8f5f7933be32a`.
No live keys, real providers, deployments, public ACP, webhooks or Check Run
publication were exercised. Live release remains operator-gated.

## Wiring and profiles

VerificationPipeline injects SourcePort, MetaPort, RoutePort, RunnerPort,
CheckPort, RunStore and Clock. compose is the explicit profile boundary:
offline binds FixtureSource/Meta/Route/Checks and the production
ReviewedFixtureRunner; live binds validated GitHub/Meta/Snap/runner factories.
The CLI refuses live in this build. Offline settings read no live credentials.

Inference/evidence use production proofhunter.bayes and proofhunter.oracle.
Oracle evaluation and provenance binding precede execution; isolation attestation
precedes observations. Charged attempts precede runner calls, complete probes
precede sealing. Workers claim 240-second leases for the 180-second budget.
Async I/O respects the original deadline. LeaseLostError prevents publication;
unexpected failures produce sanitized internal_error receipts with reloaded
durable budgets. Offline creates no check intent; live requires live mode and
the explicit checks flag. Publication is not performed by this worker.

Replay preserves parent bindings and bypasses Meta/Snap. CLI replay verifies the
receipt hash, source hashes, isolation and fresh base/head/confirmation
observations. Tamper, missing artifacts, mode mismatch or non-reproduction fail.

### Heartbeat wiring and readiness

API and worker share the same SQLite store and
`mode_config_fingerprint(settings, load_enrollments(settings.enrollments_path))`.
CLI computes the fingerprint once. Workers write at startup/start of work,
every poll iteration and via a background refresh during active work:
**TTL 30 seconds / interval 10 seconds**. Poll sleeps stay below the interval.
Worker IDs are valid opaque SafeTokens. Heartbeat write failures log once and
are suppressed; subsequent writes continue. Store time controls freshness.

`/readyz` requires ready schema, valid mode config and a fresh matching worker
heartbeat. Absent worker/dependencies, expiry or fingerprint mismatch returns
**503**, including offline mode; only all three predicates permit **200**.
API and worker must run concurrently against the same DB/effective config.

### Durable pin and provider high-water reservations

Persist the resolved RunCheckpoint.pinned_revision with reserve_progress
**before source fetch or any Meta/Snap I/O**. Replay substitutes its original
base/head before persistence. Existing durable pins are reused exactly.

Allocations are absolute durable high-water marks, including internal retries
and ambiguous outcomes, and are **never refunded**. Successful persistence
authorizes entering an adapter. Lost acknowledgement or reservation failure
authorizes no I/O; non-lease errors reach the worker's sanitized failure path
using reloaded budgets. Lease loss stops publication.

| Binding | Meta allowance per propose | Snap maximum per choose |
| --- | --- | --- |
| Offline fixtures | 0 outbound requests | 1 per call, capped at 2 total scripted requests |
| Live adapters | 2 including retries | 2 per call including retries |

Meta calls only if `max_meta_requests - reserved >= allowance`; reserve the
whole positive allowance first. Otherwise use the labeled builtin
budget_exhausted fallback without calling. Receipt usage is reserved high-water;
Meta ProviderUsage.calls is actual provenance.attempts / MetaError.attempts.

For non-replay Snap, spent is requests_spent(run_id) when present, otherwise
counted stub/live decisions. Compute
`want = min(max_snap_requests - spent, per_call)`; offline additionally caps
the total request limit at 2. If `reserved - spent < want`, first reserve
`spent + want`. Pass exactly `reserved - spent` as
DecisionState.snap_requests_remaining. Zero headroom permits policy-only routing.
Receipt Snap usage is the high-water reservation, not a refund of unused retries.

### Recovery policy

- Durable pin, zero allocations, no probes and no attempts: resume from fetch
  with the exact durable pin, never re-pin even if the PR head moved.
- Any allocation/probe/attempt with a durable pin: fail closed
  **inconclusive / revision_unavailable**, with zero Source/Meta/Snap/runner I/O.
  Preserve the public original pin, checkpoint allocations and source hashes
  from committed probes. Partial execution does not authorize safe continuation.
- Legacy checkpoint=None: fail closed without external calls; unknown Meta/Snap
  usage is conservatively charged to the run's limits.

Transparent resume after provider spend is not implemented. Recovery never
resets the deadline, refunds reservations or derives hidden pins from event text.

### Offline golden evidence

Seed 17 offline composition uses the reviewed pinned candidate bank after
production generation; other seeds/live use production generation. This is the
disclosed development bank, not an independent holdout. Buggy sentinel
`[15,15,15] @ 10%` gives expected/base/head/repeated head `41/41/42/42`:
confirmed_regression. Fixed is no_defect_found with full required coverage.
Golden histories/selections/budgets/verdicts match after normalization of IDs,
times, seals and the disclosed runner transport difference (reviewed runner
stdout/stderr hashes are null). Meta usage stays 0; Snap buggy=1, fixed=2.

## Resolved cross-owner defects

| Prior defect | Status and evidence |
| --- | --- |
| readyz heartbeat unavailable but 200 | **RESOLVED**: store/API heartbeat refreeze and shared composition; integration tests cover absent/fresh/expired/mismatched readiness and active-work refresh. |
| durable pin/allocations unavailable | **RESOLVED**: typed checkpoint/reserve_progress plus pipeline reservations; tests cover pin-only recovery, conserved partial recovery, legacy rows, ordering and reservation failures. |
| bench importing fixtures.reference | **RESOLVED**: production Bayes/oracle bench lane; search below has no matches and integrated sample bench passes with 120 rows. |

Verified static search (exit 1 means no matches):

```text
$ rg -n 'fixtures\.reference' bench --glob '*.py'
rg exit: 1 (1 = no matches)
```

**Open: independent holdout pending for G3 verifier.** Sample results are not
independent release validation.

## Lane handoff and scope audit

Verified with git show --stat: refreeze commits touched only their owned paths.

```text
e15159c190b820f1b56b7b89a6645697d1a88bfa (store)
 docs/agent-handoffs/store.md
 src/proofhunter/api.py
 src/proofhunter/store/__init__.py
 tests/store/test_progress.py
 tests/store/test_store.py
 5 files changed, 558 insertions(+), 43 deletions(-)

22400999aebdb08fd43b0fa6c4d6af408f4ac162 (bench)
 bench/arms.py
 bench/corpus/__init__.py
 bench/corpus/item_count/checkout.py
 bench/corpus/manifest.json
 bench/corpus/prepare.py
 bench/freeze.py
 bench/harness.py
 bench/policy.json
 bench/results/sample.adversarial.jsonl
 bench/results/sample.csv
 bench/results/sample.jsonl
 bench/results/sample.summary.md
 bench/run.py
 bench/scoring.py
 docs/agent-handoffs/bench.md
 tests/bench/test_benchmark.py
 16 files changed, 765 insertions(+), 239 deletions(-)
```

Integration edits are confined to pipeline/worker/CLI, integration tests, README,
this document and deployment guidance. No frozen contracts/config, store/API,
fixtures, bench or lockfile changes belong to this lane.

## Exact offline release commands (PRD §9)

```bash
export UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline
uv sync --locked
uv run --locked pytest -q
uv run --locked python -m proofhunter demo --offline --case buggy --seed 17 --receipt ./buggy-receipt.json
uv run --locked python -m proofhunter demo --offline --case fixed --seed 17 --receipt ./fixed-receipt.json
uv run --locked python -m proofhunter replay --offline --receipt ./buggy-receipt.json --probe counterexample
uv run --locked python -m bench.run --offline --split sample --seed 17 --output ./bench-results.jsonl
# Supply distinct PROOFHUNTER_TEST_API_TOKEN / PROOFHUNTER_TEST_OPERATOR_TOKEN
# as local test-only values, never live keys. Share DB and effective config.
export PROOFHUNTER_DB_PATH="$HOME/proofhunter-offline.sqlite3"
uv run --locked python -m proofhunter serve --profile offline
# In a separate terminal, with the same environment/config:
uv run --locked python -m proofhunter worker --profile offline
uv run --locked python tools/gate.py integrated --evidence DIR
```

Serve binds privately; readiness stays 503 until the matching worker runs.
Demo exit 0 means terminal evidence, not necessarily a clean verdict.
Deployment units are examples only, not installed services.

### Recorded offline verification

Final implementation runs, all using the offline environment and locked uv:

```text
pytest -q: 868 passed, 1 warning in 15.71s
ruff check .: All checks passed!
mypy src bench tools: Success: no issues found in 70 source files
mypy tests/integration: Success: no issues found in 1 source file
integrated full suite: 868 passed, 1 warning in 17.24s
[gate] demo evidence: ok
[gate] counterexample replay: ok
[gate] tampered receipt is refused: ok
[gate] sample bench rows: 120
[gate] integrated PASSED
```

Gate additionally validates 113 fixture/schema files and 251 contract tests.
The existing Starlette/httpx deprecation warning is from the locked dependency
stack; no dependency change or warning suppression was made. No pre-commit config
was present. Release rerun evidence is retained at /home/ubuntu/ph-evidence and
the top-level transcript at /home/ubuntu/ph-gate.log. No UI visual check was made.

## Remaining operator/live prerequisites

- Live GitHub App pin/authorization smoke and stale-head checks.
- Meta/Snap live smoke, retry accounting, route visibility and latency.
- Daytona isolation fingerprint, resource enforcement, teardown and tagged cleanup.
- Agentverse/ASI:One registration, mailbox, discovery and fresh-account invocation.
- Demonstrate the 180-second latency target on three staged runs.
- Check/webhook P1 gates stay off by default; separately validate publication,
  delivery, stale-head refusal and outbox draining before enabling.
- Reviewed retention sweeper (7-day operational / 30-day public receipts).
- Independent holdout pending for G3 verifier.
- Transparent resume after provider spend not implemented.
