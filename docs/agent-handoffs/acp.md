# ACP lane handoff

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
