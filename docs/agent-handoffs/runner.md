# Runner handoff

VERDICT: PASS (offline role gate only; live release remains operator-gated)

- Branch: `devin/1794078900-proofhunter/runner`
- Exact G0 parent: `5c3aecd0ec98eb7ad1a91c43d85bf940d52d67cf`
- Commit: use the full SHA in the role's final report (this document is part of
  that commit). No shared contracts, fixtures, lock or configuration changed.

## Implementation

`src/proofhunter/runner/__init__.py` exports the required
`create_runner_port(settings) -> RunnerPort`. Offline binds
`ReviewedFixtureRunner`; it never constructs Daytona or accesses provider keys.
It runs only the four hash-pinned, reviewed first-party demo modules from disk,
never request source bytes. Unknown or altered source and mismatched provenance
are denied. No production module imports `fixtures.reference` or binds
`fixtures.adapters`: only fixture catalog data is used. The reference adapter is
an adjacent-boundary stand-in in tests, not a production fallback.

`daytona.py` implements stable v0 daytona 0.231.0 with fresh snapshot creation per
observation and per attestation. Private ingress, block-all network, empty
volumes/secrets, minimal environment, fixed commands, bounded source and request
uploads, execution/control deadlines, TTL and stop/delete policies are explicit.
Snapshot CPU/memory are checked before upload; daytona 0.231.0
`CreateSnapshotParams` accepts `resources`, whereas
`CreateSandboxFromSnapshotParams` does not. Strict host parsing checks nonce, attempt, side,
commit/source/input hashes, exact keys, output size, signed integer type/range
and truncation. It rejects duplicate keys and multiple JSON envelopes. Oracle
values and receipt/ledger data never enter the sandbox.

`ops/sandbox/wrapper.py` is the trusted root-owned snapshot entrypoint. It checks
uploaded byte/input hashes, keeps the nonce outside the child, drops to a distinct
unprivileged UID with no-new-privileges, installs resource limits, and streams
both child output channels with a combined cap and deadline. It emits a separate
bound envelope and never relays child stderr. Output truncation is explicit.
The process group is killed in `finally`; full sandbox deletion is still required
because an adversarial descendant can leave a process group. The parent computes
the envelope hash; no sandbox-supplied artifact hash is trusted.

The worker always owns deletion, even after async caller cancellation. Synchronous
SDK lifecycles run in worker threads, each with bounded SDK operations; cancellation
does not terminate the thread. Drain threads before process shutdown. A durable,
fsynced ledger records allocation names before create and provider IDs afterward.
Ambiguous allocations and deletion failures remain available for cleanup.
Deletion failure yields `infra_error`, clears any value and invalidates attestation.

## Interfaces and assumptions for integration

- `RunnerPort` and all DTOs are unchanged. `observe()` returns unusable outcomes
  for target/provider/isolation failures rather than raising target exceptions.
  `attest()` must precede live observations, and must be refreshed every 15 minutes.
- The source adapter owns enrollment, exact commit-to-source binding, public repo
  authorization and fetching. The runner checks input/source hashes through the
  frozen DTO and compares snapshot/image/harness provenance against its gate.
  A source hash is not proof that GitHub served the pinned commit.
- Oracle stays on the control plane. The worker must obtain T1 oracle trust before
  invoking the runner and reserve its own invocation/confirmation budget, run
  deadline and fresh attempt IDs. Runner has no expected-result parameter.
- Offline requests use the frozen catalog enrollment and harness descriptor.
  All observations/attestations remain `offline_fixture`. No live success or
  GitHub check can be inferred from these tests.
- Live enrollment must use `runner.harness`, the SHA-256 of the checked-in wrapper
  followed by canary bytes, and version `proofhunter-harness@1`. The repo's
  `ops/sandbox` files must be present beside `src` in the deployed checkout;
  this adapter is not a self-contained wheel image builder. Install exactly
  those files in the snapshot and pin the resulting immutable image digest.
- Snapshot resources are one CPU and one GiB; per-child address space defaults
  to 256 MiB. A two-CPU request is conservatively restricted to one CPU. The
  stable SDK cannot allocate fractional-GiB snapshot memory; the child enforces
  the exact MiB limit. Snapshot mismatch is denied rather than rounded upward.
- State directory is `settings.db_path.parent`, must exist and be operator-private.
  Store/integration must preserve the ledger with persistent worker state and
  coordinate orphan sweeping against expired run leases. No RunStore methods or
  database schema changes were necessary.
- Host `stdout_sha256` hashes the trusted envelope (or the capped prefix of an
  oversized transport result), not target stdout. `stderr_sha256=None` means no
  raw stderr artifact was retained, not evidence of empty target stderr.

## Live gate and operator artifacts

No live calls were made, no provider keys read, no code deployed and no checks,
webhooks, PRs or public agents enabled. Live isolation, real CPU/memory enforcement,
actual SDK timeouts, surviving descendants, private ingress, account network tier,
digest mapping and successful deletion remain **unverified**.

See `ops/sandbox/README.md` for snapshot installation, the benign canary command,
required operator policy evidence, the local approval artifact and the tagged,
ledger-intersected quota-bound cleanup recipe. Approval is necessary but does not
skip the canary. The stable SDK does not independently prove snapshot digest:
the operator must verify the immutable snapshot-to-digest mapping and effective
policy before approving its fingerprint. A failed/unapproved gate denies all
target uploads. No setting silently selects fixture execution for a live request.

Approval now additionally requires strict integer `daytona_account_tier >= 3`
and `canary_result` containing the exact four successful checks and per-host
`probes_denied` booleans documented in the sandbox README. Tier 1/2 is refused
even if the provider echoes `network_block_all=True`. Every essential-service,
generic IP and metadata probe runs without short-circuiting. Both recorded and
fresh canary evidence are required. Missing/invalid evidence fails before
allocation; approval is rechecked before observations. Fixed sanitized local
diagnostics are exposed through `approval_refusal_reason`; frozen public DTOs
still report `isolation_unverified`. No Daytona calls were made for this change.

## Operator provisioning follow-up

The proposed one-sandbox shared-key diagnostic was cancelled before any Daytona
API call. No sandbox/snapshot was created, no probe ran and no deletion or
confirmation call was necessary. Official docs were read: Tier 1/2 organization
policy takes precedence and essential services remain reachable; Tier 3/4
supports strict sandbox block-all without an essential-service bypass. Actual
account tier and enforcement remain unverified. Do not treat docs as live evidence.

The exact post-authorization provisioning sequence is maintained in
`ops/sandbox/README.md`, under **Operator provisioning sequence** and **Preflight**:

1. Keep intake/live execution disabled; obtain a fresh resource/region/budget
   authorization and dedicated minimally scoped ProofHunter key. Never reuse the
   shared ForkSafe key or modify other projects' snapshots. Separate provisioning
   privileges from runtime privileges; secrets stay in operator process env only.
2. Independently verify tier >= 3, strict policy, private ingress and external
   controlled-host availability. Tier 1/2 or uncertainty stops provisioning.
3. Build a team-owned Python 3.12 image from a reviewed digest-pinned compatible
   base; install the exact wrapper/canary/host files root-owned and read-only,
   narrow sudoers, daytona-owned 0700 request directory and distinct nobody UID.
   Retain an immutable published image digest and verify file/harness hashes.
4. Separately authorize `client.snapshot.create(CreateSnapshotParams(name=...,
   image="registry/image@sha256:...", resources=Resources(cpu=1, memory=1, disk=3)))`.
   Confirm image fit/readiness and immutable digest mapping, with no mutable alias
   or resource fallback. The cancelled diagnostic permitted zero snapshot creates.
5. Configure that snapshot/digest and persistent private ledger state. Run only
   the bootstrap canary in a fresh labeled sandbox: `_create("preflight", 15)`,
   `_policy`, fixed `CANARY_COMMAND` with `cwd="/", env=ENV, timeout=10`, strict
   bounded JSON and `valid_canary`. Always `_delete(..., "preflight")` in
   `finally`; confirm exact allocation absence by both-label filtered list.
6. Independently validate other isolation controls before atomically writing
   0600 approval. Required exact keys: `config_fingerprint`, `image_digest`,
   `effective_policy_reviewed: true`, strict integer `daytona_account_tier >= 3`,
   and `canary_result` with four true checks and the exact all-true nine-host
   `probes_denied` map. The full JSON and per-host schema are in the README.
7. Call `await runner.attest()` for another fresh canary; require verified status
   and confirmed deletion before target uploads/intake. Refresh within 15 minutes,
   revoke on changes/failures, and drain threads on shutdown. Use the ledger,
   both labels and expired leases for bounded orphan cleanup; never sweep other
   resources by name prefix alone.

No image builder or host-side canary CLI is shipped or live-validated here. The
documented Dockerfile layout and SDK snippets require an operator-reviewed base,
registry, controlled host, real digest and new authorization before execution.

## Checks

All verification uses `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline`.

```text
uv run --locked pytest -q tests/runner tests/contracts
253 passed in 3.18s
uv run --locked ruff check src/proofhunter/runner tests/runner ops/sandbox
All checks passed!
uv run --locked ruff format --check src/proofhunter/runner tests/runner ops/sandbox
8 files already formatted
uv run --locked mypy src/proofhunter/runner tests/runner ops/sandbox
Success: no issues found in 7 source files
python3 tools/gate.py setup
[gate] setup PASSED
```

The tests use a fake stable SDK and reviewed fixture adapters at adjacent
boundaries. They cover fresh allocation, normal/empty fixture inputs, unknown
bytes, malformed/empty/spoofed/duplicate/oversized output, bool/float/overflow
values, timeout/nonzero/provider errors, denied policy, missing approval,
provenance mismatch, short deadline, cancellation and deletion failures. Wrapper
unit tests mock process/filesystem/stream I/O: they do not run untrusted code or
pretend to prove live UID/resource/network enforcement. No pre-commit config or
installed repository hooks were found; final report records the hook inspection.

Defects (setup owner): none. Blockers for offline integration: none. Live release:
operator gates pending as above.
