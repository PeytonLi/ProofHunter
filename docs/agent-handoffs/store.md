# Store and control API handoff

VERDICT: PASS

- Starting freeze: `5c3aecd0ec98eb7ad1a91c43d85bf940d52d67cf`.
- Assigned branch: `devin/1794078900-proofhunter/store`.
- All ProofHunter interactions were offline and fixture-backed. No provider keys,
  provider calls, PR code execution, deployment, publication, checks or webhooks
  were enabled in a deployed service. No frozen files were modified.

## Implementation and wiring

`proofhunter.store.create_store(settings)` returns `SQLiteStore`, implementing
both frozen RunStore and ControlStore protocols. Its constructor also accepts
Clock and IdFactory; production defaults are UTC wall clock and UUIDv4.
`proofhunter.api.create_app(ApiDependencies(...))` creates the FastAPI app.
Bind the repository validator and optional webhook verifier through their ports;
the API never imports fixture implementations or calls provider services.

SQLite uses per-operation connections, WAL, foreign keys, 100 ms busy_timeout and
three bounded BEGIN IMMEDIATE acquisition attempts. Writes and snapshot load_run
use explicit transactions. Idempotency, quota reservation and queue creation are
atomic. Same scope/key/body returns current status even after quota exhaustion;
changed body raises IdempotencyConflictError. Worker mutation transactions fence
owner, epoch, stored expiry and terminal state. Expired claims increment epoch.
Stage checkpoints validate the frozen state machine and append ordered events.
Attempt lifecycle updates preserve identity and monotonic charges; logical probes
are deduplicated separately. A failed probe transaction leaves no observations,
probe or budget update. finish revalidates the sealed hash and run provenance,
and atomically stores receipt, optional bound live check intent and terminal status.

### Migration 1

`schema_version` records applied migrations; reopen is idempotent and unknown
future versions are rejected. Tables: runs, idempotency_keys, inbound_messages,
webhook_deliveries, run_events, attempts, probes, observations, receipts,
check_outbox, quota_reservations. Indexed rolling quota reservations provide the
hourly counter. Constraints include unique scope/key, delivery ID, run/attempt ID,
run/probe number, run/input hash, run/input hash/side and one receipt/outbox per run.
Webhook delivery plus queued run plus quota reservation commit together.

## Interface assumptions

- **Requester scope:** the authenticated ACP client may supply
  `X-ProofHunter-Requester` containing its sender bucket. The API SHA-256 hashes
  that value into `acp:<digest>`; absent/empty uses `service:default`. This is a
  rate-limit bucket, not an identity assertion. Service-wide caps apply regardless.
  ACP must preserve the same bucket and inbound-message Idempotency-Key on retries.
- **Quota/budget:** Settings.quota supplies hourly, global active and per-requester
  active caps; Settings.budget initializes each run. Charged attempts and committed
  probes update recoverable sandbox/pair/confirmation usage. Quota check alone is
  advisory, matching the fixture reserve_quota semantics; intake reserves atomically.
  AdapterError has no retry-after field, so HTTP uses the fixture's 60-second retry
  hint, also returned by QuotaDecision. Disabled intake rejects new runs, not retries.
- **Webhooks:** require both settings.github.enable_webhook and an injected verifier.
  Tests explicitly inject a fake-enabled settings object, never enable a live service.
  Verify exact raw bytes before JSON parsing, validate enrollment IDs through the
  repository validator, then transactionally dedupe and enqueue. Duplicate delivery
  returns 200 even when quota is full. Event SHAs are persisted with the delivery,
  but the frozen load_run does not expose the triggering event. Integration needs
  a reviewed event accessor/stale-head policy before enabling webhook intake;
  it must not silently claim verification of the event's original head.
- **Replay:** mirror the fixture's eligible replay: only receipt.replay.probe_no is
  accepted; no eligible sealed parent returns replay_unavailable. Replays have their
  own operator bucket, idempotency key, quota reservation, run_kind and parent hash.
  load_run returns the bound Replay. Source retrieval/availability and replay command
  rebinding for the new receipt are worker responsibilities, not API provider calls.
- **Auth:** `/v1/*` uses constant-time bearer comparison. Replay POSTs accept only
  the separate operator token; service endpoints accept only the service token.
  Missing tokens fail closed. Frozen offline load_settings intentionally reads no
  tokens: fixture composition must explicitly inject test-only tokens into Settings;
  do not read live keys to make an offline server ready.
- **Readiness:** SQLiteStore.ready checks schema version; API checks that plus distinct
  configured transport tokens and offline live-feature exclusions. Fixture stores
  without ready() get a local get_status read check. There is no frozen heartbeat
  read/write port, so readyz explicitly reports `worker_heartbeat: unavailable` rather
  than claiming worker liveness. Integration must add a reviewed heartbeat contract
  before using this endpoint as worker-health evidence. No providers are contacted.
- **HTTP:** 8 KiB input and 256 KiB webhook limits include chunked requests. Errors
  use sanitized ErrorEnvelope, including framework 404/405/422 and internal 500.
  Receipts use frozen public projections, hash-derived ETags and exact-match 304.

## Setup-owner defect (non-blocking for store)

The frozen RunStore writes cannot persist a typed pinned revision or pre-call
Meta/Snap allocations, and RunRecord cannot return the original pin before a
committed probe. RunEvent.detail is sanitized text, not a pin/budget encoding;
none was invented. AttemptRecord describes sandbox attempts, not provider calls.
The reference fixture also only replaces full budget data at finish.

**Mitigation for integration:** a recovering worker finding no committed probes
must conservatively treat Meta and Snap budgets as fully spent and use labeled
fallback/policy. Because the original pin is not recoverable through frozen ports,
it must re-pin and fail closed / explicitly label the run rather than silently
accept a moved head. The exact fail-closed policy is integration's decision.

**Proposed setup change:** a lease-fenced typed checkpoint/reservation that stores
the immutable pin and monotonic provider allocations before external calls, plus
recoverable pin/allocation fields in RunRecord. Update shared fixtures/schema tests
centrally if adopted. This does not prevent use of the implemented store behavior.

## Fixtures and tests

Adjacent boundaries use FixtureRepositoryValidator, FixtureWebhookVerifier,
InMemoryRunStore, SteppingClock and SequentialIds. Sealed evidence derives from
`fixtures/demo/receipts/buggy.seed17.json`; a live-shaped mock receipt/check pair
tests atomic outbox behavior without any provider or publisher interaction.
The same intake/conflict/quota/checkpoint/attempt/probe/finish/replay scenario runs
against SQLite and InMemoryRunStore. Tests additionally cover each stale, expired,
wrong-owner and terminal worker write, lease renewal/reclaim, reopen/migrations,
concurrent intake, bounded busy handling, injected SQL rollback failures, quota
windows, budget enforcement, auth separation, body limits, public projection,
webhook enqueue/dedupe and readyz/error behavior.

## Verification

All commands use `PROOFHUNTER_PROFILE=offline UV_OFFLINE=1 uv run --locked`:

- `pytest -q tests/store`: `44 passed, 1 warning in 1.78s`.
- `pytest -q`: `249 passed, 1 warning in 3.04s`.
- `ruff check src/proofhunter/store src/proofhunter/api.py tests/store`:
  `All checks passed!`.
- `mypy src/proofhunter/store src/proofhunter/api.py`:
  `Success: no issues found in 2 source files`.
- `mypy src/proofhunter/store src/proofhunter/api.py tests/store`:
  `Success: no issues found in 3 source files`.
  The configured setup gate does not typecheck role tests; this is an additional check.
- `python tools/gate.py setup`: `205 passed in 1.26s`, `All checks passed!`,
  `Success: no issues found in 14 source files`, `[gate] setup PASSED`.
- No pre-commit configuration is present. CI calls the setup gate and, after
  pipeline exists, integrated gate. No ruff format stage is configured;
  owned Python files were formatted with ruff.
- Existing pinned TestClient stack emits one warning:
  `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated;
  install httpx2 instead.` Environment checked: correct repo .venv with fastapi
  0.162.2, httpx 0.28.1 and starlette 1.10.0. Lock/dependencies were not changed and
  the warning was not suppressed.

Persistent logs: `/home/ubuntu/proofhunter-store-{owned,tests,lint,types,test-types,gate}.log`.

## Remaining integration/operational work

No autonomous retention sweeper is installed; apply reviewed seven-day operational
and thirty-day public receipt retention in integration/operations, preserving any
archived demo replay artifacts. No live behavior or UI was visually verified.
Worker recovery mitigation, heartbeat, source availability, webhook stale-head
handling and outbox publishing/reconciliation remain integration-owned. No new
store-role blockers remain.
