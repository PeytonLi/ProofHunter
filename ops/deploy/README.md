# Deployment examples and private Render staging

## Authorized Render creation — 2026-11-07

Direct API creation returned 201 for each new service, with no retries; no other
service was touched. Both are Oregon Starter, one instance, auto-deploy off,
at creation-time commit `d0513988fbbc40ac93fecdcc6e3e34844b800e9e`.

| Name | Service ID | API type | Monthly estimate | Deploy ID / status |
|---|---|---|---|---|
| proofhunter-core | `srv-dbnp5bm7bikc73d5ofig` | private_service | $7.25 including 1 GB disk | `dep-dbnp5bu7bikc73d5ogfg` / live |
| proofhunter-acp | `srv-dbnp60rl550s73fqpnf0` | background_worker | $7 | `dep-dbnp613l550s73fqpo3g` / live; runtime publication-held restart loop |

Dashboards: [core](https://dashboard.render.com/pserv/srv-dbnp5bm7bikc73d5ofig),
[ACP](https://dashboard.render.com/worker/srv-dbnp60rl550s73fqpnf0).
Core's returned private address: `proofhunter-core:10000`; ACP API base:
`http://proofhunter-core:10000`. ACP has no inbound address. Workspace:
`tea-d892qj6k1jcs7382afo0`. Combined estimate: $14.25/month before other charges.
Subsequent documentation pushes do not redeploy either service.

Core deploy became live at `2026-11-07T21:06:59.707263Z`; all 156 log entries
observed through `21:07:13Z` were paginated, with no Python traceback. Evidence:

```text
2026-11-07T21:06:52.610171161Z runner disabled: isolation_unverified; intake disabled
```

Internal `/readyz` is **not verified**; no remote shell was used and the held
ACP does not call the API. No run was submitted. Runner/checks/webhooks and ACP
publication remain disabled. Remaining gates are internal readiness/intake
probes, sandbox isolation/execution/replay, separately approved ACP publication
and chat, and checks/webhooks. No mailbox registration was attempted.
See `docs/integration.md` and `/home/ubuntu/ph-report/render-resources.json` for
the deployment observations and sanitized evidence.

ACP deploy was marked live at `2026-11-07T21:08:03.646081Z`, but repeated
publication refusals at `21:08:10Z`, `21:08:19Z` and `21:08:42Z` show a held
runtime/restart loop, not a working agent. All 167 deployment log entries
observed through `21:08:57Z` were paginated without a Python traceback:

```text
2026-11-07T21:08:42.920342775Z configuration error: ACP publication is disabled; PROOFHUNTER_ACP_PUBLISH=true requires operator approval after the isolated three-run gate
```

The deployed ACP gate handler returns exit 2; numeric exit codes were not
exposed in the fetched Render logs/events. Publication stays false, no Agentverse
key was supplied, and the one-Starter-instance service was left unsuspended.

## Render configuration (Blueprint not applied)

`render.yaml` describes two paid services in the same Oregon workspace/network:
`proofhunter-core` (private service, one Starter instance, 1 GB disk) and
`proofhunter-acp` (Starter background worker). Matching services were created
through direct API requests; the Blueprint itself was not applied.
Core intake, runner, checks and webhooks are disabled. API run creation and
replay creation return 503 `intake_disabled` before authentication, store or
provider calls. Readiness still reports the existing store/heartbeat readiness;
it is not an assertion that execution or intake is enabled. Startup prints
`runner disabled: isolation_unverified; intake disabled`.

**ACP is intentionally not healthy with publication held:** normal startup
exits 2 before Agent construction when `PROOFHUNTER_ACP_PUBLISH=false`. Render
may restart it or mark deployment failed. This proposal does not enable mailbox
polling, endpoint/manifest publication or telemetry. Do not re-register the
existing mailbox and never supply `AGENTVERSE_API_KEY`. Publication needs
separate approval; neither this proposal nor service creation grants it.

The Blueprint obtains the private service's generated hostname and port with
`fromService: {type: pserv, name: proofhunter-core, property: hostport}`.
Because hostport has no URL scheme, its ACP start command prefixes `http://`
in process memory. The separate API-create ACP payload instead requires the
actual `http://<core-internal-hostname>:10000` after core creation. Do not assume
the generated internal hostname equals the service name. No public API URL is
needed. Both services must remain in the same workspace and region.

Render automatically installs uv when `uv.lock` exists. `UV_VERSION=0.13.7`
pins that tool; `PYTHON_VERSION=3.12.16` selects a complete Python version.
Build is `uv sync --locked`. Start uses `uv run --locked` against the lockfile.
`autoDeploy: false` is the supported deprecated Blueprint spelling (current
equivalent: `autoDeployTrigger: off`). API payloads use `autoDeployTrigger`.
The API schema still lists `starter`; current Blueprint/pricing tables instead
name its size `0.5c-512mb`. This proposal retains the requested legacy name;
server-side Blueprint validation has not been exercised.
Blueprint `pserv` maps to API `private_service`, and Blueprint `worker` maps to
API `background_worker`. Secrets use Blueprint `sync: false`; the API requires
key/value entries, so the prepared JSON contains explicit replacement markers,
not `sync` fields. Never submit those markers as real credentials.

Supply `PROOFHUNTER_ENROLLMENTS_JSON` through the operator environment, not git.
Live CLI validates it and its trust gates, then atomically replaces the external
disk registry at `PROOFHUNTER_ENROLLMENTS` with mode 0600. The parent directory
must already exist. Symlinks and paths inside this checkout are refused. API and
worker load the same persisted registry and public fingerprint. Inline content
is not printed; malformed content fails closed before startup.

Supply `GITHUB_PRIVATE_KEY` as a real multiline PEM through Render API/UI.
The operator normalizes saved secret formatting in memory only (preserve header
and footer, use 64-column body lines). Runtime parsing stays strict; no lenient
PEM parser or key-file write is added. Do not persist hydrated create payloads.

Read-only inspection found workspace `tea-d892qj6k1jcs7382afo0`, no existing
`proofhunter-*` services in the first 100, and zero build sources. These results
did not establish repository access by themselves. Subsequent authorized
creation and the successful core build verified access to the branch.
Prepared placeholder-only API requests are private VM artifacts:
`/home/ubuntu/ph-staging/render-create.json` and `render-create-acp.json`.

Published pricing is $7/month for Starter compute plus $0.25/GB/month for disk:
core approximately $7.25/month, ACP $7/month, total $14.25/month before other
workspace charges, taxes or usage. Even a publication-held ACP service may bill.
Official references: [Blueprint spec](https://render.com/docs/blueprint-spec),
[create service](https://api-docs.render.com/reference/create-service),
[private networking](https://render.com/docs/private-network),
[uv version](https://render.com/docs/uv-version), [pricing](https://render.com/pricing).

## Single-disk process and offline unit examples

For a single persistent-disk live service, use `proofhunter stack --profile live`
instead of two services. Bind privately through `PROOFHUNTER_BIND_HOST` and
`PROOFHUNTER_BIND_PORT`; configure a shared persistent `PROOFHUNTER_DB_PATH` and
external trusted `PROOFHUNTER_ENROLLMENTS`. The supervisor exits nonzero when
either API or worker stops. Keep runner, checks and webhooks off for this lane.
Live tokens and in-memory PEM belong only in the hosting secret environment.
See `docs/integration.md` for enrollment and explicit retention flags; scheduling
is off by default. The offline unit examples below are unchanged and are not
live deployment manifests. No systemd service was created or started.

The unit examples below are offline and private. Create the unprivileged service user,
install the reviewed checkout at `/opt/proofhunter`, and provision a writable
`/var/lib/proofhunter` before use. Set the same database and enrollment paths for
API and worker. Explicitly provision distinct test-only API/operator tokens for
the offline API; the template contains no credentials. Do not use live keys.
Run both units with the same effective configuration/enrollment registry so
their mode_config_fingerprint matches. `/readyz` returns 503 until a worker
writes a fresh matching heartbeat to the shared SQLite store. Workers refresh
at startup, during polling and during active work (TTL 30s, interval 10s).
Unit ordering alone does not establish readiness; monitor `/readyz` after both
processes start and after config changes.

The ACP unit is deliberately not installable as a boot service: offline ACP is
an interactive private console and refuses to start a public agent. For console
use, pass the local API test token using the ACP client's documented transport
setting. Public Agentverse registration, mailbox and production credentials need
the separate operator gates in `docs/provider-smoke.md` and the ACP handoff.

No unit was installed, enabled or started. Bind privately; do not expose the API
to the Internet. No webhook/check publication, cleanup sweeper or retention job
is claimed. Heartbeat wiring is implemented; these example units were not run.
Apply reviewed retention and tagged sandbox cleanup
policies only after those operational interfaces have been reviewed.
