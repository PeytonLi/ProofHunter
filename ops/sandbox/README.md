# Operator-only Daytona sandbox gate

Do **not** run the wrapper/canary on the worker or development machine. No live
commands in this directory are part of CI. The adapter uses daytona **0.231.0**
stable v0, not v2. Live mode and `PROOFHUNTER_ENABLE_LIVE_RUNNER` must both be on.

Authorization for the proposed shared-key diagnostic was withdrawn before any
Daytona API call. No sandbox or snapshot was created. The procedure below is
documentation only, **not authorization to execute**. Keep the live runner disabled
until a new, explicit provisioning/canary authorization and all gates below pass.

## Official policy findings

The [network limits documentation](https://www.daytona.io/docs/en/network-limits)
states for Tier 1/2: "Organization-level network restrictions take precedence over
sandbox-level settings" and "Essential services remain reachable."
For Tier 3/4: "Essential services do not bypass a sandbox allow list or block-all
setting." These statements explain why returned flags are not enforcement proof;
they do not establish this account's actual tier or observed network behavior.

The [Python SDK documentation](https://www.daytona.io/docs/en/python-sdk/sync/daytona/)
documents default-snapshot creation, `network_block_all`, private `public=False`,
custom `labels`, minute-based `auto_stop_interval`, and
`auto_delete_interval=0` as "delete immediately upon stopping."
`delete(..., wait=True)` waits for destruction. Default snapshots are suitable
only for a separately authorized diagnostic, not the reviewed runner image gate.

## Operator provisioning sequence (new authorization required)

1. Disable intake and live runner execution. Obtain separate authorization for
   the exact image registry, account/region, snapshot name, sandbox count and
   cleanup budget. Do not reuse or modify ForkSafe/TrueForge resources. Create a
   dedicated ProofHunter organization/project and minimal-permission key with
   only the required sandbox create/read/exec/delete access. Use a separately
   scoped provisioning identity if snapshot/image creation requires broader
   privileges. If the provider cannot scope the existing shared account safely,
   stop rather than reuse its key. Inject keys only into the operator process
   environment; never into image layers, build args, files, sandbox env or logs.
2. Verify account tier >= 3 and effective strict network policy using independent
   operator evidence. Stop on Tier 1/2, uncertain tier, public ingress, or any
   essential-service exception. Record evidence without credentials. Confirm a
   real operator-owned HTTPS canary host resolves and accepts connections from
   outside Daytona. This is not permission to run the canary locally.
3. In an approved isolated build environment, use a team-owned,
   Daytona-compatible Python 3.12 Linux base pinned as `registry/base@sha256:...`.
   Review its pinned dependency inventory: it must already contain
   `/usr/bin/python3`, `/usr/bin/sudo`, `/usr/bin/setpriv`, the `daytona` user,
   nobody UID/GID 65534, and provider-required runtime support. No runtime package
   installation, hooks, secrets or background processes sharing UID 65534.
   Use only the reviewed wrapper/canary bytes and controlled-host file in the
   build context. A derived Dockerfile must perform the equivalent of:

   ```dockerfile
   FROM <reviewed-team-base>@sha256:<verified-base-digest>
   USER root
   COPY --chown=0:0 wrapper.py canary.py canary-host /opt/proofhunter/
   RUN chmod 0755 /opt/proofhunter && chmod 0444 /opt/proofhunter/* \
       && install -d -o daytona -g daytona -m 0700 /var/proofhunter \
       && test ! -e /tmp/proofhunter-target
   COPY --chown=0:0 proofhunter-sudoers /etc/sudoers.d/proofhunter
   RUN chmod 0440 /etc/sudoers.d/proofhunter \
       && /usr/sbin/visudo -cf /etc/sudoers.d/proofhunter
   USER daytona
   ```

   `proofhunter-sudoers` must contain exactly these grants (no broader inherited
   grants may remain for `daytona`):

   ```text
   daytona ALL=(root) NOPASSWD: /usr/bin/python3 -I /opt/proofhunter/wrapper.py
   daytona ALL=(root) NOPASSWD: /usr/bin/python3 -I /opt/proofhunter/canary.py
   ```

   Build and publish only through approved team infrastructure; retain the
   registry's immutable manifest digest, build provenance and file hashes.
   Never pass a mutable tag to the snapshot API. The Dockerfile above is a
   required layout example, not a tested image builder shipped by this repo.
4. With the newly authorized provisioning identity, register one uniquely named
   reviewed snapshot (the earlier cancelled diagnostic authorized **zero**):

   ```python
   from daytona import CreateSnapshotParams, Resources

   snapshot = client.snapshot.create(
       CreateSnapshotParams(
           name=reviewed_unique_snapshot_name,
           image=reviewed_registry_reference_at_sha256_digest,
           resources=Resources(cpu=1, memory=1, disk=3),
       ),
       timeout=120,
   )
   ```

   Resources use whole CPU/GiB units. Confirm this disk size meets the image's
   actual requirements before provisioning; stop if the reviewed image does not
   fit. Do not silently resize/fallback. Wait for readiness and independently
   verify the immutable name-to-image-digest mapping. Do not replace an existing
   snapshot alias. Snapshot creation is a separate authorized operation, never a
   side effect of sandbox creation via `CreateSandboxFromImageParams`.
5. Configure the worker's exact snapshot, image digest, persistent private state
   directory and reviewed harness hash. Instantiate the runner with dedicated
   credentials in a controlled operator process with intake still disabled.
   Before any approval file exists, run the bootstrap canary lifecycle below;
   never bypass it by writing a speculative approval. Review exit status,
   bounded exact JSON, returned resources/private ingress and all probe results.
   Always delete and verify absence using labels plus the exact allocated ID/name
   in the ledger. Any incomplete probe, exception or cleanup failure blocks release.
6. Independently verify CPU/memory/time limits, child UID/no-new-privileges,
   surviving-descendant cleanup, no secret injection, private ingress, bounded
   output and source-upload binding with separately authorized benign tests.
   Do not upload repository/target code as part of the network bootstrap.
   Only then atomically install the exact approval JSON documented below in the
   operator-private state directory (file mode 0600). Record the actual tier and
   actual successful canary output; no unknown keys or truthy substitutes.
7. Run `await runner.attest()` for a second fresh canary with approval in place;
   require verified status and successful deletion. Only after this gate, source
   enrollment/provenance and the independent isolation checks may intake/live
   observations be enabled. Refresh within 15 minutes. Revoke approval and disable
   intake on policy/image/harness/tier change or any cleanup/isolation failure.
   Keep credentials off the sandbox and drain lifecycle threads before shutdown.

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

In daytona 0.231.0, `CreateSnapshotParams` accepts `resources`;
`CreateSandboxFromSnapshotParams` does not. Provision the
snapshot with 1 CPU and 1 GiB memory; the adapter inspects these values before
upload. Per-invocation child memory is at most 1024 MiB (default 256 MiB) and
process count at most 64 (default 16). A request for two CPUs still runs on the
stricter one-CPU snapshot. There is no resource fallback to provider defaults.

## Preflight, separate authorized live session only

1. Inspect the account's effective strict block-all policy (including essential
   services), private ingress, snapshot image digest, absence of injected secrets,
   sudo rules and resource configuration. Record effective policy and account
   tier evidence in provider-smoke. Tier 1/2 organization policy overrides
   sandbox block-all and allows essential services: **do not approve Tier 1/2**.
   Tier >= 3 and denied probes are both mandatory; an echoed block-all flag
   proves neither. The SDK cannot cryptographically establish
   the image digest: the operator must verify the immutable snapshot-to-digest
   mapping. Never attest a mutable snapshot alias.
2. The benign commands in that **fresh snapshot only** are:

   ```text
   /usr/bin/sudo -n /usr/bin/python3 -I /opt/proofhunter/canary.py
   /usr/bin/sudo -n /usr/bin/python3 -I /opt/proofhunter/wrapper.py
   ```

   Run only the canary first. It checks DNS, controlled HTTPS, known essential
   services, public IP HTTPS, link-local metadata TCP access, secret-like env
   names and restricted UID. All four top-level JSON booleans and every value
   in `probes_denied` must be true. All probes run even after a failure. A canary is
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
   {
     "config_fingerprint":"<runner.fingerprint>",
     "image_digest":"sha256:<64 hex>",
     "effective_policy_reviewed":true,
     "daytona_account_tier":3,
     "canary_result":{
       "egress_denied":true,"metadata_denied":true,
       "secrets_absent":true,"restricted_child":true,
       "probes_denied":{
         "github.com":true,"raw.githubusercontent.com":true,"pypi.org":true,
         "files.pythonhosted.org":true,"registry.npmjs.org":true,"1.1.1.1":true,
         "app.daytona.io":true,"169.254.169.254":true,"169.254.170.2":true
       }
     }
   }
   ```

   This is a trusted operator artifact, not a provider response or PR input.
   Live startup with the runner enabled validates this same local file and
   refuses missing or invalid evidence without constructing a provider client.
   Runner-disabled private staging does not require this artifact.
   `attest()` still allocates a fresh benign canary sandbox, checks returned
   policy/resources, executes the canary and confirms deletion. Approval alone
   cannot make it pass. Missing approval or any mismatch denies source upload.
   Observations require an attestation less than 15 minutes old and unchanged
   approval. Refresh it before expiry. Revoke approval on policy changes.
   Record the actual operator-reviewed canary JSON, not a copied success template.
   `runner.approval_refusal_reason` exposes only fixed sanitized diagnostics:
   `approval_invalid`, `account_tier_invalid` (missing/non-integer/nonpositive),
   `account_tier_insufficient`, `canary_result_missing`, or
   `canary_result_not_denied` (malformed, incomplete or any non-true result).
   Frozen public attestation reason remains `isolation_unverified`.

There is no standalone host-side launcher in this directory. After separate live
authorization, an operator must build the image and create its immutable named
snapshot via `client.snapshot.create(CreateSnapshotParams(..., resources=...))`.
No image or snapshot name is supplied by this repository: configure the reviewed
snapshot and its verified image digest in settings. A bootstrap canary (before
writing approval) uses `DaytonaRunner._create("preflight", 15)`, then verifies
`_policy(sandbox)`, calls
`sandbox.process.exec(CANARY_COMMAND, cwd="/", env=ENV, timeout=10)`, and always
`_delete(sandbox, "preflight")` in `finally`. These private helpers describe the
existing lifecycle, not a public CLI. `_create` calls
`client.create(CreateSandboxFromSnapshotParams(...), timeout=15)` with the
configured snapshot, private ingress, block-all, empty volumes/secrets, minimal
environment and the labels below. `_delete` calls
`client.delete(sandbox, timeout=5, wait=True)` and records deletion in the ledger.
After approval, public `await runner.attest()` repeats this fresh lifecycle; it
will not launch a bootstrap canary without approval. Never upload target source
for the bootstrap or preflight.

For clarity, `_create("preflight", 15)` uses the equivalent stable SDK parameters
below and durably records its random allocation name before calling the API:

```python
CreateSandboxFromSnapshotParams(
    name=ledger_recorded_unique_name,
    snapshot=reviewed_unique_snapshot_name,
    public=False,
    network_block_all=True,
    env_vars=ENV.copy(),
    labels={"proofhunter.deployment": runner.deployment, "proofhunter.run": "preflight"},
    volumes=[], secrets={}, os_user="daytona",
    auto_stop_interval=1, auto_delete_interval=0, ttl_minutes=2,
)
```

The bootstrap must check `_policy` before `exec`, require exit code zero, parse
at most 4096 bytes using `strict_json`, and require `valid_canary(result)`.
Use `try/finally` around the whole allocation/execution lifecycle and do not let
an execution/parse failure skip deletion. Retain ambiguous allocations in the
ledger; never retry create blindly. After deletion, fully consume
`client.list(ListSandboxesQuery(labels={"proofhunter.deployment": runner.deployment,
"proofhunter.run": "preflight"}), request_timeout=15)` and confirm that the exact
allocated sandbox is absent. Do not delete other matching resources merely
because their labels match. Keep only timestamp, sanitized operation/status,
latency, exposed request IDs and short hashed sandbox identity in diagnostic
evidence, never headers, provider bodies or raw credentials.

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
