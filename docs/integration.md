# Integration release: offline G2

## Verified private live staging — 2026-11-07

At integration commit `d84d86c`, two authorized real-provider runs completed on
this VM. No whole-run retries were made. The API is loopback-only on port 8765;
API and worker share `/home/ubuntu/ph-staging/runs.sqlite3` and the external
mode-0600 registry. Enrollment repository/installation identity **matched** the
authorized demo. `/healthz` and fresh-heartbeat `/readyz` returned **200**.

Exact process commands run from the checkout (the private instrumentation
wrapper calls the production CLI, sets the shared paths, binds 127.0.0.1:8765,
forces all three feature flags false, reconstructs the collapsed PEM in memory,
and records allowlisted HTTP metadata only):

```sh
uv run --locked python /home/ubuntu/ph-staging/launch.py enroll --profile live --repo-url https://github.com/PeytonLi/ProofHunter-Demo --out /home/ubuntu/ph-staging/enrollments.json --unprovisioned-runner
uv run --locked python /home/ubuntu/ph-staging/launch.py serve --profile live > /home/ubuntu/ph-staging/api.log 2>&1
uv run --locked python /home/ubuntu/ph-staging/launch.py worker --profile live > /home/ubuntu/ph-staging/worker.log 2>&1
uv run --locked python /home/ubuntu/ph-staging/launch.py retention --profile live
uv run --locked python /home/ubuntu/ph-staging/launch.py replay --profile live --receipt /home/ubuntu/ph-staging/pr1-receipt.json
```

Secret placeholders were bound via the process tool's `env`, never command-line
arguments or files. Serve received `PROOFHUNTER_API_TOKEN=<service secret>` and
`PROOFHUNTER_OPERATOR_TOKEN=<operator secret>`. Worker/enroll/retention/replay
received `MODEL_API_KEY=<Meta secret>`, `SUBQ_API_KEY=<Snap secret>`,
`GITHUB_APP_ID=<App ID>` and
`PROOFHUNTER_GITHUB_APP_PRIVATE_KEY=<collapsed PEM secret>`; the wrapper exports
`GITHUB_PRIVATE_KEY` in process memory. No Daytona credential was bound.
Equivalent direct CLI entrypoints are documented below; the instrumentation
wrapper is private VM state, not a packaged deployment dependency.

Runs were submitted through authenticated `POST /v1/runs` with stable distinct
idempotency keys, polled to completion and fetched through the receipt API.
Both sealed receipts independently verified:

| PR | Execution mode | Verdict / reason | Meta | Snap | Sandbox observations |
|---|---|---|---|---|---|
| PeytonLi/ProofHunter-Demo #1 | live | inconclusive / isolation_unverified | live, 1 call, 0 fallbacks | live, 1 call, 0 fallbacks | 0 |
| PeytonLi/ProofHunter-Demo #2 | live | inconclusive / isolation_unverified | live, 1 call, 0 fallbacks | live, 1 call, 0 fallbacks | 0 |

Pins matched exactly:

- #1 base `aa8cfe2b3ec9f438363bc7de3e6aed6b4fce5db4`, head `d9d6443c5b3266cc8087b5fb30e5fe5c5198fdf6`.
- #2 base `b2c8b83c9c150e6d85aeb5771290a95bb5410fcd`, head `9e4ed8d954edc6573cc3e592cadb64c5becb4efb`.

Runner remained disabled with the explicit unprovisioned sentinel, not fixture
snapshot/image values. There were zero sandbox invocations/probes, zero SQLite
observations, no sandbox ledger and no Daytona HTTP calls. Meta/Snap each made
exactly one HTTP request per run. GitHub made 20 total requests: five enrollment,
eight first-run (including token exchange), seven second-run (cached token).
Receipt GitHub `calls=0` is an existing pipeline accounting limitation; the
observer, durable pins and fetched source hashes provide actual call evidence.

API operator `POST /v1/retention/purge` with `{}` returned 200 and defaulted to
dry-run. CLI dry-run matched: all 15 count categories were zero. No apply was
performed. Replay exited 1: `live refused: isolation_unverified`, with no
additional provider calls.

Shared API/worker fingerprint:
`287788db7f021811abcdff862758d9a3b85f84d9ce4bab8abfd7cc397ea300e2`.

| Provider | UTC request start | HTTP | Header latency ms | Sanitized request ID |
|---|---|---|---|---|
| GitHub enrollment/pin/fetch, 20 calls | 20:57:15–20:57:58 UTC | 200/201 | 154–444 | Full per-call IDs in evidence.json |
| Meta PR #1 | 2026-11-07T20:57:36.018790Z | 200 | 14867 | bfa920bf-7e68-44e1-86ef-e31cef5c14a5 |
| Snap PR #1 | 2026-11-07T20:57:50.933266Z | 200 | 437 | req_proxy_mw6vxgl4_zag6ab |
| Meta PR #2 | 2026-11-07T20:57:58.260747Z | 200 | 19135 | 6958830d-745e-4ea7-a931-9b62a357ed5c |
| Snap PR #2 | 2026-11-07T20:58:17.428516Z | 200 | 318 | req_proxy_mw6vy0zy_rq8ks5 |

Audit latency ends at response headers, not full parsing. Enrollment rows were
assigned the resulting mode fingerprint after enrollment completed. Complete
24-row public-safe call metadata is at `/home/ubuntu/ph-staging/evidence.json`
and `/home/ubuntu/ph-report/evidence.json`; summary at
`/home/ubuntu/ph-report/staging-report.md`. Evidence, report and process logs
passed credential-pattern scanning. Raw provider bodies were not logged.

Remaining live gates: independently provisioned/approved runner isolation and
real execution/replay; public hosting and ACP/ASI:One discovery/chat; separately
approved checks/webhooks; stack hosting and restart behavior. This staging
proves real pin + Meta + Snap preparation, **not regression detection or clean
coverage**. Existing API-role runner restriction and missing shared contract
hash constant remain as documented below. No implementation change was needed.

## Historical offline release baseline

Starting SHA: `77c8062b17527c22d22d19d786a8f5f7933be32a`.
No live keys, real providers, deployments, public ACP, webhooks or Check Run
publication were exercised. Live release remains operator-gated.

## Wiring and profiles

VerificationPipeline injects SourcePort, MetaPort, RoutePort, RunnerPort,
CheckPort, RunStore and Clock. compose is the explicit profile boundary:
offline binds FixtureSource/Meta/Route/Checks and the production
ReviewedFixtureRunner; live binds validated GitHub/Meta/Snap/runner factories.
The CLI now supports explicitly selected live entrypoints. Offline settings read no live credentials.

## Live integration entrypoints (offline-tested only)

All commands below use the same `PROOFHUNTER_DB_PATH` and trusted
`PROOFHUNTER_ENROLLMENTS`. Keys, including `GITHUB_PRIVATE_KEY`, remain in memory.
No live provider calls, deployment or sandbox approval were performed for this change.

```
proofhunter serve --profile live
proofhunter worker --profile live
proofhunter stack --profile live
proofhunter enroll --profile live --repo-url https://github.com/OWNER/REPO --out /private/enrollment.json --unprovisioned-runner
proofhunter enroll --profile live --repo-url https://github.com/OWNER/REPO --out /private/enrollment.json --snapshot-id PROVEN_SNAPSHOT --image-digest sha256:DIGEST
proofhunter demo --profile live --pr-url https://github.com/OWNER/REPO/pull/N --base BASE_SHA --head HEAD_SHA --receipt /private/receipt.json --seed 17
proofhunter replay --profile live --receipt /private/receipt.json
proofhunter retention --profile live
proofhunter retention --profile live --apply
proofhunter worker --profile live --retention-dry-run --retention-hours 24
proofhunter stack --profile live --retention-apply --retention-hours 24
```

Serve loads API-role settings (control tokens, no GitHub/provider secrets); its
validator is a local trusted enrollment lookup, not a network discovery service.
Worker, enrollment, demo, replay and retention load worker-role settings. Stack
loads each half separately, shares the DB, and exits nonzero when either half
stops. API and worker fingerprints match with identical public configuration;
credentials and role do not affect the fingerprint. Stack checks this explicitly.
Keep checks, webhooks and runner disabled in this lane. API role itself rejects
the live-runner flag, so runner-enabled separate API/stack hosting requires a
config-owner decision; no role restrictions were bypassed here.

Startup rejects any enabled fixture identity, snapshot or image. The known
offline catalog identifiers are explicit denylist constants; no fixture module
is imported on a live path. Unprovisioned enrollment uses
`runner-unprovisioned` / `sha256:` followed by 64 zeros. Runner-enabled startup
rejects these or settings/enrollment runner-pin mismatches. Explicit pins are an
operator assertion of provisioning, not proof by this CLI; Daytona's independent
approval/canary gates still apply. Enrollment refuses existing outputs and paths
inside this checkout (including symlink-resolved paths), writes mode 0600 and
prints only public metadata plus enrollment fingerprint.

Enrollment discovery uses the production GitHub HTTP/session factory: App-auth
installation lookup, a repository-name-restricted bootstrap token, then a
repository-ID-restricted token and public metadata verification. The existing
repository validator cannot discover unenrolled repositories (it only looks up
the registry), so discovery lives in the owned CLI. Missing production contract
hash constant is worked around by an owned reviewed `CONTRACT_SHA256` constant;
all other contract/version identifiers come from production contracts and the
harness hash comes from the checked-in runner wrapper. No fixture read is used.

Demo requires exact lowercase 40-character SHAs. The pipeline compares them
against the durable pin after persistence and before source fetch or Meta/Snap;
mismatches produce revision_unavailable and a nonzero CLI exit. It never replaces
the actual PR pin with caller-supplied SHAs. Use an idle dedicated demo DB to
ensure run_once claims the new run, not an older queued request.

With runner disabled, real pin/fetch and Meta/Snap preparation still run, but
Daytona has no client and attestation is unverified. The pipeline stops before
TESTING, ExecutionRequest construction or observe/upload. Receipts retain live
execution mode and report isolation_unverified honestly. Replay first verifies
the sealed receipt, refuses isolation_unverified or missing counterexample/replay
bindings, then checks fetched source hashes, fresh attestation and observations.

Retention defaults to dry-run, bounded to 500 rows per category per invocation.
Worker scheduling is off unless either retention flag is explicit, accepts
intervals 1–168 hours, and runs only after an idle claim (never during its own
work lease). Errors are sanitized, logged once and suppressed. Other workers may
still be active; store retention predicates and transactions remain authoritative.

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
