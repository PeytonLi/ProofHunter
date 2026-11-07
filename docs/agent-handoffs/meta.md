# Meta adapter handoff

Implemented `proofhunter.meta.create_meta_port(settings, *, transport=None,
sleep=asyncio.sleep, clock=time.monotonic)` returning a structural `MetaPort`.
`build_request(context, model)` is pure and credential-free; `MetaError` extends
`AdapterError` with `attempts`. Settings alone supply model and credentials;
the URL is frozen. No tools, redirects, environment proxies, or provider logging.

## Errors, retry and fallback

| Condition | Reason | Retry |
| --- | --- | --- |
| Offline or absent key | provider_unavailable (disabled) | No; zero attempts |
| Timeout / overall deadline | provider_timeout | Once if time remains |
| Transport, 429, 5xx | provider_unavailable | Once if time remains |
| Other HTTP status | provider_unavailable | No |
| Refusal or content filter | provider_refused | No |
| Invalid context, byte cap, JSON/schema/taxonomy/reference/tool failure | provider_invalid_response | No |

Two HTTP attempts maximum, 20-second per-request timeout, 45-second overall
deadline including sleep/streaming; Retry-After numeric seconds or HTTP date is
capped at two seconds. Context is revalidated, common credential patterns are
redacted in source and rationales, serialized requests and responses are capped
at 64 KiB, inner output at the contract's 16 KiB. Duplicate modes keep the first.
All proposals (including duplicates) must pass local scope validation.

Integration must bind `FixtureMeta` in offline mode, not treat this disabled
live adapter as an offline success. Live success provenance is `live`, with
configured model, safe response ID, measured latency, attempts and null reason.
Caller must charge error/success attempts against `BudgetLimits.max_meta_requests`
before further work; adapter performs its own one retry, so do not wrap it in
another retry loop. Bayes/integration applies `builtin_hypothesis_payload()` on
failure and constructs `HypothesisPlan` with `ProviderProvenance(provider=meta,
mode=fallback, reason_code=error.code, attempts=error.attempts, model_id=None,
request_id=error.request_id, latency_ms=None)`. Never label fallback live.

## Examples and operator gate

Mocks: `fixtures/providers/meta_request.json`, `meta_response.json`, and
`meta_plan_payload.json`. The request adds typed `contract_text` and `diff` to
the fixture user JSON; system policy and structured schema are identical.
Tests fetch the demo PlanningContext through FixtureSource. Mock responses with
`tool_calls`/`function_call` set to null or [] are accepted; any non-empty tool call
is rejected and never executed.

`UV_OFFLINE=1 uv run --locked python -m proofhunter.meta --dry-run` (also the
default) prints sanitized request JSON without reading credentials or networking.
It prints no Authorization header or key. No module under `src/proofhunter/meta`
imports `fixtures`; the smoke CLI embeds the public buggy demo head text and the
contract rule (a test pins it byte-equal to the reviewed fixture revision).
`create_meta_port(..., max_attempts=1)` disables the retry for the smoke gate.

Live smoke is operator-only on a separate staging host, per
`docs/provider-smoke.md`: provision all required live settings securely, set
`PROOFHUNTER_PROFILE=live`, `MODEL_API_KEY`, `META_MODEL=muse-spark-1.3`, then run
`UV_OFFLINE=1 uv run --locked python -m proofhunter.meta --live`. This sends
exactly one HTTP request (no retry for the smoke gate), prints only sanitized
provenance and exits nonzero on failure. Record sanitized request ID, UTC time,
status, latency, non-secret configuration fingerprint, entitlement and approved
data handling. Confirm max_tokens and strict schema support; fallbacks fail the
gate. No live smoke was run during implementation.

## Known limits

Credential pattern matching is defense in depth, not a proof that arbitrary
source is safe to disclose; callers must withhold uncertain or unapproved
context and apply labeled fallback. Error code invalid_response also covers
invalid outbound context because the frozen taxonomy has no context error.
Only the pinned chat-completion protocol is supported. No provider entitlement,
schema-subset compatibility, or real latency is claimed by mock tests.
No pre-commit configuration exists in the frozen repository.
