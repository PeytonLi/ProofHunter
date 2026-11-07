# Deployment examples, not deployed

All examples are offline and private. Create the unprivileged service user,
install the reviewed checkout at `/opt/proofhunter`, and provision a writable
`/var/lib/proofhunter` before use. Set the same database and enrollment paths for
API and worker. Explicitly provision distinct test-only API/operator tokens for
the offline API; the template contains no credentials. Do not use live keys.

The ACP unit is deliberately not installable as a boot service: offline ACP is
an interactive private console and refuses to start a public agent. For console
use, pass the local API test token using the ACP client's documented transport
setting. Public Agentverse registration, mailbox and production credentials need
the separate operator gates in `docs/provider-smoke.md` and the ACP handoff.

No unit was installed, enabled or started. Bind privately; do not expose the API
to the Internet. No webhook/check publication, cleanup sweeper, retention job or
worker heartbeat is claimed. Apply reviewed retention and tagged sandbox cleanup
policies only after those operational interfaces have been reviewed.
