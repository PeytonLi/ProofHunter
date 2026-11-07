# Store and control API handoff

VERDICT: PASS_OFFLINE. Live composition/publication remains cross-owner work.

- Base: `4140bf0dfab8d090181b6aea37cbb7e574eb75a6`.
- Branch: `devin/1794083400-proofhunter-live/live-store`.
- Owned changes: `src/proofhunter/store/`, `src/proofhunter/api.py`, `tests/store/`,
  and this handoff. Shared contracts, oracle, ports, configuration, dependency
  files and benchmark snapshots are unchanged.
- All verification was offline and fixture-backed. No provider requests, live PR
  execution, publication, deployment or live visual verification was performed.

## Composition and existing guarantees

`create_store(settings)` returns `SQLiteStore`, implementing RunStore,
ControlStore and HeartbeatStore. Clock and IdFactory remain injectable.
`create_app(ApiDependencies(...))` accepts offline or live settings and injected
repository validation, webhook verification, store, heartbeat store and matching
configuration fingerprint; it does not contact providers at startup/readiness.

SQLite uses WAL, foreign keys, 100 ms busy_timeout and three bounded
BEGIN IMMEDIATE acquisition attempts. Intake/idempotency/quota and sealed
receipt/optional outbox/terminal status commit atomically. Worker mutations fence
owner, epoch, stored expiry and nonterminal state. Recovery preserves the exact
pin and monotonic provider allocation counters; legacy NULL checkpoints fail
closed. Attempt lifecycle updates preserve identity/monotonic charges, while
logical observations and probes are separately deduplicated.

## Schema v4

The constructor applies ordered transactional migrations from v1/v2/v3, reopens
idempotently and rejects unknown versions. `ready()` requires v4.

1. Runs, idempotency keys, inbound messages, webhook deliveries, run events,
   attempts, probes, observations, receipts, check_outbox, quota_reservations and
   quota/active-run indexes.
2. Durable worker_heartbeats and nullable runs.checkpoint. Historical v1 rows
   retain checkpoint=NULL rather than receiving a fabricated allocation ledger.
3. Outbox check ID, owner/epoch/expiry, in-flight operation, reconciliation flag,
   call/ambiguity counters, append-only outbox_attempts; immutable UPDATE triggers
   on receipts, observations, run_events, probes and outbox_attempts. Terminal run
   status/provenance UPDATEs are rejected, including JSON containing receipt hash.
4. A singleton main-schema retention_gate and retention-only DELETE triggers on
   those five append-only tables. New runs also persist their trusted enrollment
   snapshot in runs.enrollment; replay intake copies it. Historical rows remain
   NULL where the original enrollment was never captured; migration does not
   invent historical configuration. Existing receipt/pins/source hashes/target
   remain unchanged, and the frozen contract's enrolled source is checkout.py.
   Terminal enrollment updates are also fenced.

Terminal run owner/expiry cleanup remains permitted: finish stores terminal status
then clears its worker lease. Publish state/check IDs/history are separate from
the immutable run and receipt. DELETE protection is separate from UPDATE
protection, so reviewed retention does not require disabling immutability triggers.

## Step A: auth, readiness and original-input replay

- Every `/v1/*` request returns sanitized 401 if either transport credential is
  missing or they are equal. Constant-time bearer comparison is retained.
- Service routes accept only API token; replay and retention POSTs accept only
  operator token. ACP receives only API token/API base, never operator token.
- `/healthz` is process liveness. Internal `/readyz` checks schema, distinct
  configured credentials, offline/live-flag exclusions and a fresh heartbeat
  matching profile/fingerprint. Missing heartbeat store/fingerprint, absent,
  expired, future or mismatched heartbeat fails closed with sanitized 503.
- Heartbeats use trusted store time, bounded TTL, clock-regression rejection and
  deterministic newest matching selection. CLI already injects the store and
  mode_config_fingerprint; VerificationWorker refreshes before claims and while
  working/idle. Live CLI startup is still rejected outside this lane.
- Replay intake delegates eligibility/integrity to oracle.validate_replay for any
  eligible probe number, not only the designated counterexample. The new replay
  run binds the stored original SHAs/input/source hashes, parent receipt hash and
  its own quota/idempotency reservation. Missing/corrupt bindings produce sanitized
  409 replay_unavailable. Replay requests cannot override pins or source hashes.

Other routes remain POST /v1/runs, GET /v1/runs/{run_id}, GET
/v1/runs/{run_id}/receipt (sealed projection/ETag/304) and optional POST
/webhooks/github (disabled unless configured and verifier injected). JSON is
bounded at 8 KiB; webhook bodies at 256 KiB, including chunked requests. Public
errors use ErrorEnvelope without provider details. X-ProofHunter-Requester is a
hashed quota bucket, not a separate identity assertion. Webhooks verify raw HMAC
before parsing and atomically deduplicate/enqueue; event-head recovery remains an
integration concern.

## Step B: durable publisher lifecycle

Checks remain default off (`PROOFHUNTER_ENABLE_GITHUB_CHECKS=false`). Claims return
None unless settings explicitly enable checks AND profile is live. Mutations also
recheck this fence; offline can never publish through these store methods.
finish's existing optional intent creation behavior is unchanged.

Publisher API:

1. claim_outbox(owner, lease_seconds=30) returns a fenced OutboxLease, or None.
2. begin_outbox_attempt(lease, CREATE/RECONCILE) reserves durably BEFORE external I/O.
3. mark_outbox_published(lease, check_run_id, http_status=...) persists a successful
   create and check ID; record_outbox_attempt records RETRYABLE or AMBIGUOUS.
   mark_outbox_ambiguous is the ambiguity shortcut.
4. After an ambiguous/interrupted create, CREATE is refused until a reserved
   reconciliation completes. reconcile_outbox requires original head_sha and
   external_id, successful bounded lookup status, and either the verified check
   ID or a definitive absence result. A match publishes without another create;
   definitive absence permits a later create. Uncertain create failures cannot
   be marked directly retryable; only definitive HTTP 429 can retry directly.
5. Five external-call reservations maximum; two ambiguous outcomes cause held
   state for operator review. Lease expiry/reclaim preserves uncertainty and
   rejects stale owners/epochs. No automatic held-state release is provided.

outbox_attempts(run_id) returns append-only numbered UTC audit events: operation,
bounded sanitized outcome, optional validated HTTP status. Start and completion
are separate immutable records, not rewritten provider responses. No provider
bodies, arbitrary messages or credentials are stored. No publisher loop/provider
calls were added; the publisher must perform and verify the actual lookup.

## Step C: bounded retention

`purge_expired(now, operations_ttl=timedelta(days=7),
receipts_ttl=timedelta(days=30), batch_limit=500, dry_run=False)` returns sanitized
per-table deletion counts plus retained_outbox and retained_replay_parents counts.
Batch limit is 1..5000. Time must be timezone-aware; TTLs positive and ordered.

- One BEGIN IMMEDIATE transaction; at most batch_limit deletions per table per
  call, deterministic terminal-time/run-ID/row order. Repeated calls converge.
- Nonterminal runs and live worker/outbox leases are protected. Active replay
  ancestors are protected transitively, including their operational records.
- At seven days from terminal updated_at, delete run events, attempts,
  observations/probes, intake/delivery/idempotency records and terminal quota
  reservations. Settled outbox history is eligible; pending/held history remains.
  Heartbeats expire from retention seven days after their heartbeat expiry.
- Retain runs, sealed receipt (including original probes/replay/source hashes),
  target/checkpoint and enrollment snapshot until receipt retention. A ten-day
  original receipt still creates a 202 replay with its original bindings.
- At thirty days, remove settled/unclaimed outbox, receipt and finally run only
  after remaining FK children are removed. Pending/claimed/held or unknown outbox
  state is retained and counted. Any retained replay child protects its parent's
  receipt/run; later calls can remove parents after children expire and are purged.
  Reported retained-parent counts describe blockers at the start of receipt cleanup.
- After complete expiry, receipt GET is 404 and new replay intake is 409. Bounded
  cleanup may require multiple calls before an entire expired run can disappear.
- Protected-table DELETEs require retention_gate.enabled=1. Purge sets/clears it
  inside its writer transaction. Failure rolls back deletes AND gate changes.
  Dry run simulates the same bounded deletes under a savepoint and rolls them back,
  returning counts without changing data or leaving the gate enabled. This gate
  protects ordinary code/ad-hoc DELETEs, not a malicious administrator who can
  explicitly modify the gate/schema.

Operator endpoint: `POST /v1/retention/purge` with `{}` defaults to dry_run=true,
batch_limit=500. `{ "dry_run": false, "batch_limit": 500 }` applies cleanup.
It uses trusted injected clock/default TTLs, returns only dry_run and count maps,
rejects extra fields and enforces the same missing/equal/cross-token auth fences.
No scheduler is installed.

## Verification

All commands prefixed `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked`:

- pytest -q: `980 passed, 1 warning in 21.00s`.
- ruff check .: `All checks passed!`.
- mypy src bench tools tests/integration:
  `Success: no issues found in 73 source files`.
- mypy tests/store: `Success: no issues found in 5 source files`.
- python tools/gate.py setup: `271 passed in 2.67s`, `[gate] setup PASSED`.
- python tools/gate.py integrated: `980 passed, 1 warning in 16.15s`, demos for
  buggy/fixed, counterexample replay, tamper refusal and 120 sample bench rows;
  `[gate] integrated PASSED`.

Store tests cover credential fences, injected live readiness, arbitrary eligible
replay, missing bindings, populated v1/v2/v3 upgrades, terminal immutability,
outbox concurrency/fencing/lost acknowledgements/reconciliation/retry bounds,
retention age boundaries, active/replay-parent protection, settled versus held
outboxes, bounded convergence, dry run, operator auth and SQL rollback/gate safety.
No pre-commit configuration or active hooks found. uv.lock/pyproject.toml unchanged.
The existing StarletteDeprecationWarning about httpx/testclient remains; correct
repo .venv and locked dependency versions were checked, not modified/suppressed.

## Cross-owner follow-ups

- **cli.py live composition:** bind real validator/webhook/provider dependencies
  and enable live CLI only through authorized gates. Preserve common fingerprint
  computation and worker heartbeat injection/refresh.
- **Publisher integration:** call fenced outbox methods, reserve before I/O,
  recheck current PR head, perform commit/external-ID reconciliation and honor
  held state. The existing GitHub adapter's in-memory ambiguity set alone is not
  durable integration with this outbox. No network I/O belongs in DB transactions.
- **Retention scheduler:** call the store/operator endpoint from reviewed cli/worker
  scheduling; run bounded batches until converged and monitor retained blockers.
- **In-memory PEM:** GitHub/config owners must handle in-memory App private-key PEM
  support; no credentials or configuration files were changed in this lane.
- **Source availability:** pre-fetch replay 409 checks are store/API-level only:
  missing/corrupt archived bindings are rejected without providers. GitHub source
  deletion/access loss cannot be detected here without external fetch; worker
  fetch/provenance checks and original enrollment/archive availability remain
  integration/operator responsibilities. Keep any operator demo bundle outside
  database retention if replay after receipt expiry is required.
- **Legacy enrollment snapshots/webhooks:** old rows without captured enrollment
  still need the reviewed registry/archive; webhook intake lacks enrollment in
  its frozen port. Do not fabricate historical configuration or claim the event's
  original head without reviewed recovery wiring.
