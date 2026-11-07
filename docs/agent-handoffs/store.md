# Store handoff — BLOCKED

- Starting freeze: `5c3aecd0ec98eb7ad1a91c43d85bf940d52d67cf`.
- Assigned branch: `devin/1794078900-proofhunter/store`.
- Profile: `offline`; no provider calls, keys, deployment, PR execution or publication.
- Deliverable: this defect report only. No SQLite implementation, migrations, API or
  role tests were added. Do not integrate this branch as a completed store role.

## Frozen setup defect: recovery cannot preserve pins and provider budgets

Owner: **setup**, in `src/proofhunter/ports.py` and the corresponding frozen DTOs,
fixtures and schema tests. This is a correctness blocker, not a failing setup gate.

PRD §2 requires the worker to resolve **and record** exact SHAs, recover committed
progress, conservatively charge ambiguous allocations, and enforce maximum Meta
and Snap requests including retries before spending. The assigned store must expose
that persistence through the shared worker ports, not a parallel private protocol.

The complete `RunStore` mutation surface is `claim`, `renew`,
`checkpoint(lease, RunEvent)`, `record_attempt(lease, AttemptRecord)`,
`commit_probe(lease, ProbeRecord)` and `finish(lease, ReceiptV1, CheckIntent | None)`.
None accepts pinned revisions or provider budget reservations before a receipt.
`RunEvent` contains only run ID, sequence, timestamp, kind, from/to state, reason
and a sanitized 200-character detail. `AttemptRecord` is specifically a sandbox
attempt with a required side and input hash, not a model-provider allocation.

`RunRecord` returns only run ID, kind, target, seed, status, parent receipt hash,
replay, probes, attempts and events. In particular it cannot return a previously
pinned revision before the first committed probe. The reference fixture does not
fill this gap: `_new_run` initializes budget usage to zero, checkpoint only changes
state/time, record_attempt only changes attempts_used, commit_probe only appends a
probe, and budget is replaced only by finish.

Failure scenarios:

1. Worker pins head A, calls Meta, then loses its lease before any probe. A resumed
   worker receives neither the pin nor the charged Meta request via load_run. If
   the PR moved, re-pinning can silently change the run to head B.
2. A worker spends its Meta/Snap allowance and crashes before finishing. Provider
   usage in RunStatus.budget remains zero, so recovery cannot enforce the total
   call limit including ambiguous in-flight requests using the frozen interface.

Checked possible alternatives: probes retain observations but are absent in the
first scenario; attempts do not represent provider calls; finish is too late;
RunEvent.detail is not a typed persistence channel for pins/budget and has no
shared encoding. Inventing such an encoding would silently add a cross-owner
contract. No workaround was implemented.

### Required setup-owner resolution

Freeze a lease-fenced checkpoint/reservation DTO and port that atomically persist
the immutable pin and monotonic budget usage before external calls. Include the
pin and recoverable allocation state in RunRecord, update InMemoryRunStore and
contract/schema tests, then issue a new exact freeze. The setup owner should
decide the exact signature with integration; this branch does not edit frozen
files or presume that design decision.

## Interface assumptions / remaining work

No new assumptions were made about requester scope, webhook enqueue, readiness,
retention, replay source retrieval or worker heartbeat. Those need documenting
when implementation resumes. Quota numbers must come from Settings.quota and
per-run limits from Settings.budget. create_store/settings and create_app/deps
remain unimplemented. Webhooks/checks must remain independently disabled by
default. Adjacent-boundary fixtures inspected: InMemoryRunStore,
FixtureControlApi, FixtureRepositoryValidator, FixtureWebhookVerifier,
SteppingClock and SequentialIds. No fixtures were changed or executed as PR code.

## Verification

Commands ran from the assigned repository and exact freeze, before this docs-only
commit. Logs live outside the worktree under `/home/ubuntu/`.

- `git fetch origin`: exit 0; branch and HEAD matched the assigned branch/freeze.
- `PROOFHUNTER_PROFILE=offline uv sync --locked`: exit 0, pinned dependencies installed.
  Log: `/home/ubuntu/proofhunter-store-sync.log`.
- `PROOFHUNTER_PROFILE=offline UV_OFFLINE=1 uv run --locked pytest -q`:
  `205 passed in 1.73s`. These are existing contracts, not store implementation tests.
  Log: `/home/ubuntu/proofhunter-store-tests.log`.
- `PROOFHUNTER_PROFILE=offline UV_OFFLINE=1 uv run --locked python tools/gate.py setup`:
  `205 passed in 1.41s`, `All checks passed!`,
  `Success: no issues found in 14 source files`, `[gate] setup PASSED`.
  Log: `/home/ubuntu/proofhunter-store-gate.log`.
- Python introspection of RunStore signatures, dataclass fields and DTO fields:
  `REPRODUCED: no typed pin persistence/resume field or pre-finish provider-budget write`.
- No `.pre-commit-config.yaml` is configured. CI delegates to the setup gate,
  and to the integrated gate only once pipeline.py exists. No ruff format stage
  is configured in CI or the gate.
- Owned tests/lint/type checks were not run: no owned Python implementation or
  test paths exist. This is explicitly **not** a store verification pass.
- UI/HTTP behavior was not implemented or visually verified.

## Verdict

**BLOCKED** pending setup-owner contract correction. Passing existing setup checks
does not establish the required recovery behavior and must not weaken the PRD.
