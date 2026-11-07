# ACP lane handoff

## Live ACP follow-up (2026-11-07)

Implementation PASS; Agentverse account connection and ASI:One live gate BLOCKED.
Base: `4140bf0dfab8d090181b6aea37cbb7e574eb75a6`; branch:
`devin/1794083400-proofhunter-live/live-acp`.

The entrypoint now uses `load_acp_settings`: only profile, API base, service token,
seed and agent port are passed to the existing `ServiceRole.AGENT` loader.
Provider/source/operator credentials and unrelated configuration are ignored.
Live startup requires an explicit `PROOFHUNTER_API_BASE`, seed and service token.
It constructs the real bearer-only `HttpControlApiClient`; no fixture fallback.
`PROOFHUNTER_AGENT_STORAGE_DIR` optionally selects the persistent directory.
uAgents 0.26.1 has no Agent storage-directory parameter: the standalone process
changes cwd before Agent construction, so both SDK state and the reply ledger use
that directory. Use an absolute, private writable directory and one process per seed.
No secret values are placed in its JSON ledger. The seed is always supplied explicitly.

Run with `PROOFHUNTER_PROFILE=live`, `AGENT_SEED_PHRASE`,
`PROOFHUNTER_API_TOKEN`, `PROOFHUNTER_API_BASE`, optionally `AGENT_PORT` and
`PROOFHUNTER_AGENT_STORAGE_DIR`, then `uv run --locked python -m proofhunter.acp`.
Give this process no provider, GitHub, Daytona or operator keys.
`python -m proofhunter.acp --address` prints only the seed-derived address and
does not instantiate an Agent or make network calls.

Derived-from-saved-seed public address (NOT account-connected or verified discoverable):
`agent1qt7qj4vrjwxjax7cd4n4ajgn9mrf97ke2lzaau72p8f4exydamxdxhxru5y`.
Identity references are `secret:personal:AGENT_SEED_PHRASE` and
`secret:personal:PROOFHUNTER_API_TOKEN`; never paste their values into documentation.

### Exact connection and discovery prerequisites

Operator login: open `https://agentverse.ai/`, sign in to your own account, then
run the process locally with the SAME saved seed. Open
`https://agentverse.ai/inspect/?uri=http%3A//127.0.0.1%3A8001&address=agent1qt7qj4vrjwxjax7cd4n4ajgn9mrf97ke2lzaau72p8f4exydamxdxhxru5y`
(or the inspector link printed by the SDK if port differs). The browser must be
able to reach the local process. Choose Connect and Mailbox and approve the
connection while signed in. No Agentverse login/API key is available to this work;
this step was NOT performed. An ASI:One account is also needed for the fresh-chat gate.

SDK verification: in uagents 0.26.1, inspector POST `/connect` receives
`AgentverseConnectRequest(user_token, agent_type="mailbox", endpoint=None, team=None)`.
Its handler constructs `RegistrationRequest(address, name, handle, url, profile,
endpoints, protocols, metadata)` and calls
`uagents.mailbox.register_in_agentverse(request, identity, prefix, agentverse,
agent_details)`. That function GETs the identity challenge, POSTs the signed proof,
and POSTs agent details, using the user token as a bearer credential. This is the
SDK-supported headless primitive, not a `register(api_key=...)` method on Agent.
No headless CLI is added: no registration credential exists and the inspector
already exposes the supported path. Never pass a token on a command line or log
the SDK response detail (it contains provider-controlled text).

The official launch guide also describes Agents → Launch an Agent → Connect Agent
→ Chat Protocol, with a public HTTPS endpoint and a generated registration script.
That endpoint-based flow is distinct from this mailbox/inspector flow; do not expose
the private control API to satisfy it. Source read:
https://innovationlab.fetch.ai/resources/docs/next/agentverse/agentverse-sdk-uagents
and installed SDK `agent.py`, `mailbox.py`, `storage/__init__.py` (0.26.1).

After connection, set a unique available handle (suggested `proofhunter`, not
claimed), an owned public avatar, README, capability keywords and public toggle.
Keep the agent active and complete at least ten genuine interactions before
checking ASI:One discoverability; this is a prerequisite, not evidence it is listed.
Respect the free-tier periodic login requirement (at least every 30 days).
Use name `proofhunter`, description `AGENT_DESCRIPTION` from `agent.py`, and the
full README text from `src/proofhunter/acp/AGENT_README.md`. Public-safe keywords:
GitHub, pull request, PR verification, regression, counterexample, evidence receipt,
rounding bug, Agents That Prove Their Work. Do not imply all live gates have passed.
Use the real demo URLs `https://github.com/PeytonLi/ProofHunter-Demo/pull/1` and
`https://github.com/PeytonLi/ProofHunter-Demo/pull/2` only once API enrollment has
the exact original base/head pins; never use the moving demo-base tip.

### Local mailbox smoke (not an end-to-end live verification)

An eight-second local mailbox process used only the two saved role credentials,
live profile and loopback API base. No control API request was sent (no inbound
chat); no API server was needed. SDK logs were suppressed; aiohttp instrumentation
recorded no bodies or secret headers. No account registration/Connect request was
made. Normal SDK startup can automatically advertise the protocol to Almanac;
that is NOT a connected mailbox or ASI:One publication pass.
Observed statuses (UTC, status, latency ms; request IDs unavailable):

```
2026-11-07T20:38:45.153275Z 404 184
2026-11-07T20:38:45.175083Z 200 234
2026-11-07T20:38:45.238855Z 200 303
2026-11-07T20:38:45.238975Z 200 301
2026-11-07T20:38:46.335896Z 404 181
2026-11-07T20:38:47.529579Z 404 192
2026-11-07T20:38:48.706278Z 404 176
2026-11-07T20:38:49.905222Z 404 198
2026-11-07T20:38:51.105686Z 404 198
```

404 polling is consistent with an unconnected mailbox, not successful delivery.
Endpoints/methods were not recorded, so the 200s are not attributed to a specific
operation. No ASI:One messages, paid deployment, target execution or provider call
was performed. The process was terminated after eight seconds, not left serving.
Public config fingerprint inputs: uagents=0.26.1, mailbox=true,
publish_manifest=true, profile=live, API base=http://127.0.0.1:8000, port=8001.
Fingerprint SHA-256 (semicolon-separated key=value inputs in the order above):
`8bc8f88386a9558675efbc40dec3c6054cb35488c2c5dbe0793febfdc5d8b556`.

Cross-owner findings: the supplied base already supports `ServiceRole.AGENT` and
in-memory GitHub PEM; the predecessor's claims that these are missing are stale.
The remaining ACP entrypoint bug was using the default ALL role and implicit API
base. No config.py edit was needed. Deployment, enrollment pins, runner isolation,
provider gates, Agentverse account connection and ASI:One discovery remain external
prerequisites. No recurring charges or deployment are authorized here.

Checks: owned ACP suite `108 passed in 1.68s`; full suite `898 passed, 1 warning in
14.40s`; Ruff `All checks passed!`; aggregate mypy `Success: no issues found in 72
source files`; setup and integrated gates PASSED. The existing Starlette/httpx
deprecation warning is unchanged; no dependencies were changed. No pre-commit
configuration was found. ACP behavior is text-only; no visual verification done.

The sections below describe the original offline lane; its fixture URLs are not
live enrollment instructions and its historical branch is not this follow-up.

Branch `devin/1794078900-proofhunter/acp`, based on the G0 freeze
`5c3aecd0ec98eb7ad1a91c43d85bf940d52d67cf`. Owned paths only: `src/proofhunter/acp/`,
`tests/acp/`, this file. No frozen contract, fixture, schema or `uv.lock` change.

## What is implemented

| Module | Purpose |
| --- | --- |
| `acp/commands.py` | Pure, bounded parser (`parse_command`): 1024 chars, 32 tokens, no control characters. `verify <url>` (also a bare URL) accepts exactly one canonical `https://github.com/OWNER/REPO/pull/N`; `status`/`evidence` take a lowercase UUIDv4 (uppercase input is normalized); `help`. Everything else is `InvalidCommand(reason)`; nothing is guessed. |
| `acp/client.py` | `HttpControlApiClient` / `create_control_client(settings)`: the frozen `ControlApiClient` port over HTTPX. Bearer service token, `Idempotency-Key` on POST, no redirects, `trust_env=False`, 512 KiB response cap, 10 s timeout (3 s connect), one retry for transport errors and 502/503/504, `Retry-After` on 429/503, frozen `ErrorEnvelope` mapped to `ControlApiError(code, retryable, http_status, retry_after_seconds)`. Responses are validated as `RunStatus`/`ReceiptV1`; mismatched run IDs and unsealed receipts are rejected. Run IDs are validated before URL interpolation. Live profile requires a token and HTTPS unless the host is private. |
| `acp/render.py` | Plain-text, capped (3500 chars) replies from public DTO fields only. Queued reply gives the full run ID and `status <run_id>`; status gives phase, attempts and budget while active, then verdict, SHAs, evidence counts, provider modes and reason; evidence gives minimal input plus expected/base/head/repeated head, oracle version, route, replay command and receipt hash for confirmed mismatches, or coverage, missing tags, limits and receipt hash otherwise. `offline_fixture` is always labelled. Errors show only the frozen reason code. |
| `acp/service.py` | `ChatService.reply_for(sender, msg_id, text)`: dedup ledger, local rate limits, in-flight cap, overall 20 s timeout, sanitized errors. |
| `acp/agent.py` | Official uAgents wiring: `Protocol(spec=chat_protocol_spec)`, `ChatMessage` handler acks `msg_id` first, then sends `TextContent` + `EndSessionContent`; `ChatAcknowledgement` handler never replies (no loops). Published manifest, description and `AGENT_README.md`. |
| `acp/__main__.py` | `python -m proofhunter.acp`. Live: mailbox agent from `AGENT_SEED_PHRASE`. Offline: refuses to start the public agent (exit 2); `--local` chats over stdin/stdout against `PROOFHUNTER_API_BASE`. |

### Idempotency and redelivery

- The control API key for `verify` is `acp-<sha256(sender)[:16]>-<inbound msg_id>`, so a
  redelivered message reaches the same run even if local state is lost, and two senders
  cannot collide.
- The final reply for each (sender, msg_id) is stored in `ctx.storage` (uAgents
  persistent JSON store), bounded to 1024 entries and 7 days. A redelivery replays the
  stored text with a deterministic reply `msg_id` and makes no API call.
- Transient failures (timeouts, 5xx, rate limits) are not stored, so a redelivery retries.
- Concurrent redeliveries of the same message are serialized by a per-key lock.

### Local limits

Per sender: 20 commands/min and 3 verifies/10 min; global 240 commands/min; 16 in-flight
API calls. They only protect the ACP process; quotas and the hard spend cap are enforced
by the control API (PRD section 9).

## Interface assumptions (adjacent boundaries)

- Control API (owner: API lane) exposes exactly PRD section 9: `POST /v1/runs` returns
  200 or 202 with `RunStatus`; `GET /v1/runs/{id}` returns `RunStatus`;
  `GET /v1/runs/{id}/receipt` returns the sealed `ReceiptV1`, or HTTP 409 with
  `ErrorEnvelope` code `receipt_pending` while active; errors use `ErrorEnvelope`; 429
  and 503 may carry integer `Retry-After`.
- Same idempotency key with the same body returns the same run; a different body returns
  `conflicting_idempotency_key`. Requester scope for quotas comes from the service token,
  not from chat text.
- Enrollment and PR-shape refusal is authoritative on the API; the ACP parser only
  rejects non-canonical URLs early.
- `RunStatus.detail` may be absent; rendering degrades to phase/attempts/verdict.
- Tests drive `HttpControlApiClient` through `httpx.MockTransport` backed by the frozen
  `fixtures.adapters.FixtureControlApi` (real store semantics, quotas, idempotency) and
  use the frozen fixture receipts `fixtures/demo/receipts/{buggy,fixed}.seed17.json`.

## Agentverse mailbox and metadata (manual, operator only)

Not done in automation. With the live profile and the control API running privately:

1. Set `AGENT_SEED_PHRASE` (stable, secret; it fixes the agent address),
   `PROOFHUNTER_API_BASE` (private origin) and `PROOFHUNTER_API_TOKEN` in the ACP
   process only. Keep `mailbox` enabled (`Settings.agent.mailbox`, default true).
2. `uv run --locked python -m proofhunter.acp`. The log prints the agent address and an
   Agentverse inspector link. Open the inspector, choose Connect, then Mailbox, and sign
   in to Agentverse. The mailbox then buffers messages while the process is offline.
3. On the agent's Agentverse page check: name `proofhunter`, the description from
   `agent.py`, the README from `acp/AGENT_README.md` (keywords included), and the
   protocol list shows the Agent Chat Protocol (manifest published by
   `agent.include(..., publish_manifest=True)`). Add the handle, avatar and the public
   demo repository link there.
4. Make the agent discoverable (public) only after the smoke test below passes.

## Fresh ASI:One smoke steps (manual)

1. Open a new ASI:One chat (no prior context). Search for ProofHunter or address the
   agent by its handle/address.
2. Send `help`; expect scope, the four commands and the four verdicts.
3. Send `Verify https://github.com/proofhunter-demo/checkout-demo/pull/1`; expect
   `State: queued`, a full run ID and `status <run_id>`.
4. Resend the same message from the same chat if the client allows; expect the same run ID.
5. Send `status <run_id>` until completed; expect phase/attempts, then verdict
   `confirmed_regression`, short SHAs, evidence counts and provider modes.
6. Send `evidence <run_id>`; expect the minimal input with expected 41, base 41, head 42,
   repeated head 42, the replay command and receipt hash. In a live run the mode line must
   say `live`; `offline_fixture` must never appear in a live demo.
7. Send `Verify https://github.com/someone/else/pull/1` and `hello`; expect a refusal and
   the invalid-command help, with no run ID.

## Verification (offline)

```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q tests/acp
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check src/proofhunter/acp tests/acp
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src/proofhunter/acp tests/acp
UV_OFFLINE=1 python3 tools/gate.py setup
```

## Known limits

- No live agent was created, registered or messaged; mailbox and ASI:One steps are
  documented, not executed.
- `--local` mode needs a running control API; there is no in-process fallback, by design
  (no silent fixture mode).
- The stdin/stdout mode uses in-memory dedup only.
