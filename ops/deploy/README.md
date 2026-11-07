# Deployment examples, not deployed

For a single persistent-disk live service, use `proofhunter stack --profile live`
instead of two services. Bind privately through `PROOFHUNTER_BIND_HOST` and
`PROOFHUNTER_BIND_PORT`; configure a shared persistent `PROOFHUNTER_DB_PATH` and
external trusted `PROOFHUNTER_ENROLLMENTS`. The supervisor exits nonzero when
either API or worker stops. Keep runner, checks and webhooks off for this lane.
Live tokens and in-memory PEM belong only in the hosting secret environment.
See `docs/integration.md` for enrollment and explicit retention flags; scheduling
is off by default. The offline unit examples below are unchanged and are not
live deployment manifests. No service was created or started.

All examples are offline and private. Create the unprivileged service user,
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
