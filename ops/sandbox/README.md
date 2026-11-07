# Operator-only Daytona sandbox gate

Do **not** run the wrapper/canary on the worker or development machine. No live
commands in this directory are part of CI. The adapter uses daytona **0.231.0**
stable v0, not v2. Live mode and `PROOFHUNTER_ENABLE_LIVE_RUNNER` must both be on.

## Reviewed snapshot

Build a team-owned Python 3.12 image with no secrets, host mounts, volumes, shared
caches, package installation or startup hooks. Install `wrapper.py` and `canary.py`
as root-owned, non-writable files at `/opt/proofhunter/`. Pin a real controlled DNS
name (not a nonexistent name) in root-owned `/opt/proofhunter/canary-host`.
Preflight needs working operator-side evidence that this name normally resolves
and the controlled HTTPS endpoint is reachable outside the sandbox. Otherwise a
failed DNS lookup is not evidence of filtering.

Create `/var/proofhunter` owned by `daytona`, mode 0700. Permit that account sudo
**only** the two exact commands below (no arguments, no wildcard sudo rule).
The wrapper uses a distinct nobody UID 65534 with empty supplementary groups and
no-new-privileges. No other snapshot processes may use that UID. Reserve the
`/tmp/proofhunter-target` path; it must not exist in the snapshot. The target gets
only integer input on stdin, not the nonce or binding file. The root parent owns
the output envelope. Resource limits cap address space, CPU, processes, files,
core dumps and both output streams; a new sandbox is always deleted afterward.

Stable snapshot creation parameters **do not accept resources**. Provision the
snapshot with 1 CPU and 1 GiB memory; the adapter inspects these values before
upload. Per-invocation child memory is at most 1024 MiB (default 256 MiB) and
process count at most 64 (default 16). A request for two CPUs still runs on the
stricter one-CPU snapshot. There is no resource fallback to provider defaults.

## Preflight, separate authorized live session only

1. Inspect the account's effective strict block-all policy (including essential
   services), private ingress, snapshot image digest, absence of injected secrets,
   sudo rules and resource configuration. Record effective policy and account
   tier evidence in provider-smoke. The SDK cannot cryptographically establish
   the image digest: the operator must verify the immutable snapshot-to-digest
   mapping. Never attest a mutable snapshot alias.
2. The benign commands in that **fresh snapshot only** are:

   ```text
   /usr/bin/sudo -n /usr/bin/python3 -I /opt/proofhunter/canary.py
   /usr/bin/sudo -n /usr/bin/python3 -I /opt/proofhunter/wrapper.py
   ```

   Run only the canary first. It checks DNS, controlled HTTPS, known essential
   services, public IP HTTPS, link-local metadata TCP access, secret-like env
   names and restricted UID. All four JSON booleans must be true. A canary is
   necessary, not sufficient: additionally verify actual timeout, CPU/memory,
   descendant termination, upload, private ingress and deletion on the account.
   An unavailable endpoint does not establish isolation by itself.
3. Instantiate `DaytonaRunner.from_settings(settings)` in the sandbox worker.
   Read its `fingerprint` and `harness` (SHA-256 of wrapper bytes then canary
   bytes). Enroll that harness hash and version `proofhunter-harness@1`. The
   deployment identifier is the first 24 hex digits of the canonical SHA-256 of
   the absolute database path. Use the same DB path and state directory on
   restarts. The state directory must already exist and be operator-private.
4. Only after review, write `runner-isolation-approval.json` beside the database:

   ```json
   {"config_fingerprint":"<runner.fingerprint>","image_digest":"sha256:<64 hex>","effective_policy_reviewed":true}
   ```

   This is a trusted operator artifact, not a provider response or PR input.
   `attest()` still allocates a fresh benign canary sandbox, checks returned
   policy/resources, executes the canary and confirms deletion. Approval alone
   cannot make it pass. Missing approval or any mismatch denies source upload.
   Observations require an attestation less than 15 minutes old and unchanged
   approval. Refresh it before expiry. Revoke approval on policy changes.

## Tagged orphan cleanup recipe

The append-only `runner-ledger.jsonl` beside the DB records allocation names
**before** create, provider IDs after create, and confirmed deletions. Keep this
file durable with the worker state. Each sandbox has labels
`proofhunter.deployment=<deployment>` and `proofhunter.run=<run_id>` (or
`preflight`). Creation names begin `proofhunter-` and include 128 random bits.

Operator/sweeper algorithm (separate live authorization):

1. Disable intake and drain/stop the worker. Read the ledger under the same
   deployment; form outstanding names/IDs from allocated/created rows not known
   deleted. Include ambiguous create allocations; resolve their exact names.
2. Use stable `client.list()` with deployment-label filtering and paginate fully.
   For each candidate, require **both labels**, an exact name or ID in this
   deployment's ledger, and an expired run lease (or a stopped worker). Re-fetch
   candidate metadata and recheck immediately before deleting. Never delete
   based on name prefix, age or deployment label alone.
3. Apply a hard per-sweep quota (e.g. 15), call
   `client.delete(sandbox, timeout=5, wait=True)`, and append a `deleted` row only
   after success. Retain failures for the next operator sweep; do not manufacture
   a cleanup success. Log only IDs, run labels and sanitized errors.

The adapter also requests stop-after-one-minute, immediate auto-delete on stop
and a two-minute TTL as defense in depth, not proof of deletion. SDK timeouts
can be ambiguous. A create timeout leaves the allocation name in the ledger.
Cancellation of the async caller does not cancel the synchronous lifecycle:
the worker thread retains cleanup ownership, with SDK deadlines and a five-second
deletion reservation. Do not terminate the worker before draining those threads;
on process loss use the ledger recipe. Deletion failure makes an observation
`infra_error` and invalidates its attestation.
