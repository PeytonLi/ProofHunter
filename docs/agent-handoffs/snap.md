# Snap 1.1 route adapter

## Implemented

`proofhunter.snap.create_route_port(settings, *, client=None, transport=None,
clock=time.monotonic) -> SnapRoute` implements `RoutePort.choose(state, eligible)`.
Only the configured, frozen SubQ origins are used. No live provider request was
made during this work; all test HTTP uses `httpx.MockTransport` and a fake key.

Requests offer exactly the eligible strategy keys, with fixture-pinned criteria
and instructions. State contains only the structured bounded contract, step,
remaining pairs, observed/missing coverage, eligible unseen counts, last outcome,
and belief. `contract`, `coverage`, and `remaining_pairs` preserve the provider
fixture shape; additional structured state fields enrich that minimal fixture.
Run IDs, source text, and raw repository context are not sent.

Each HTTP attempt has a three-second total deadline and HTTPX timeout, no redirect
following, and a 16 KiB decoded response limit. Responses validate the answer type,
exact offered choice, optional model, probabilities and confidence. Booleans are
not numbers, nonfinite numbers are rejected, and accepted floats round to twelve
decimal places. Probabilities must cover exactly the offered keys and normalize
within the shared 0.01 tolerance. IDs pass the shared `SafeToken` validator before
being recorded. Non-live decisions never carry provider request IDs. Logs contain
only routing choices, alternatives, mode, reason, final status, sanitized ID and
elapsed latency, never request/response bodies, headers, credentials or errors.

## Integration assumptions and public API

- Startup must continue binding `fixtures.adapters.FixtureRoute` for the offline
  profile. This factory is the real adapter, not a profile-aware dispatcher. An
  offline `load_settings` supplies no key, so this adapter cannot call a provider
  with ordinary offline settings either. Tests explicitly inject fake settings.
- `requests_spent(run_id: str) -> int` reports actual decision HTTP attempts,
  including retries and failed responses. The worker must account for the delta
  around each `choose`, not infer attempts from the decision mode. Provider
  failures consume requests too. Persist usage conservatively on interruption.
- One adapter instance must survive all steps for each run. Request counters and
  the retry-used set are in memory, isolated by run ID, and retained for the
  adapter lifetime. Calls are serialized by an async lock to prevent overspending.
  Use this instance on one event loop; it is not cross-process coordination.
  A resumed worker must supply the persisted remaining request budget; retry-used
  state is not persisted by this adapter, so do not recreate it mid-run if the
  worker needs to preserve the one-retry guarantee across that recreation.
- Only steps 1 and 2 can call Snap. Allowance is the minimum of state remaining
  requests and the adapter's remaining configured budget (capped at three).
  Exactly one eligible strategy takes precedence and returns `policy /
  single_eligible`; exhausted budget or later steps return `policy / call_budget`.
  Empty or duplicate eligible tuples raise `ValueError` as programming errors.
- At most one transient retry is spent per run, and only when another request is
  available. No backoff sleep; the worker owns its overall 180-second deadline.
  The latency includes both attempts when retried. All fallbacks choose the first
  eligible member of `STRATEGY_FALLBACK_ORDER`.
- `await check_model_visibility() -> bool` is an explicit operator-only GET of
  `settings.snap.models_url`. It returns whether `data[].id` includes the configured
  model, and false on missing credentials, malformed payload or provider failures.
  It does not retry or spend run decision budget. `choose` never calls it.
- An injected `AsyncClient` remains caller-owned and is not closed. Otherwise
  each request creates and closes its client; environment proxies are disabled.
  An injected transport and monotonic clock support deterministic testing.

## Status and validation mapping

| Condition | Mode / reason | Retry |
|---|---|---|
| Valid 200 choice | live / provider_choice | no |
| 403, 404, 410 | fallback / preview_unavailable | no |
| 401 or missing key | fallback / provider_unavailable | no |
| 402 insufficient credits | fallback / provider_unavailable | no |
| 422 invalid request / non-decision model | fallback / invalid_response | no |
| 429 | fallback / rate_limited | once per run if budget permits |
| 5xx or HTTPX transport failure | fallback / provider_unavailable | once per run if budget permits |
| HTTPX or total deadline timeout | fallback / provider_timeout | once per run if budget permits |
| Other status, invalid JSON, oversized or malformed response, bad model/confidence | fallback / invalid_response | no |
| Non-string, wrong-case, or unoffered choice | fallback / invalid_choice | no |
| Invalid optional probabilities | fallback / invalid_probabilities | no |

## Offline proof and checks

`tests/snap/test_snap.py` has 73 mocked cases covering request shape for two and
three eligible strategies, fixture parsing, all status mappings, validation,
sanitized logs, no-call policies, latency, retries, per-run isolation, concurrent
budget enforcement, visibility, and caller-owned clients.

The pinned `fixtures/demo/candidate_bank.seed17.json` is validated with the shared
`CandidateBank` loader. Two valid mocked live choices produce different actual
`CandidateBank.pool_for` pools and shared `pool_sha256` values:

```text
constraint 34ce593f03f398bdf613ce7816eafb4c59537e5234824f1a883ab835f75e9e99
boundary 1a02d2e6a1ae385a06c6e82b630eb6e85d9c3a57833d6d4ed60fc82d51571346
```

All commands below ran with `PROOFHUNTER_PROFILE=offline` and `UV_OFFLINE=1`:

```text
uv run --locked pytest -q tests/snap
73 passed in 0.29s

uv run --locked ruff check src/proofhunter/snap tests/snap
All checks passed!

uv run --locked mypy src/proofhunter/snap tests/snap
Success: no issues found in 2 source files

uv run --locked python tools/gate.py setup
[gate] setup PASSED

uv run --locked pytest -q
278 passed in 1.81s
```

No frozen contract or dependency changes were needed. No simulator modifications
were made: its offline receipts must not claim live provider modes, and its current
mode-based counting cannot represent retries. The worker should use the accessor
above rather than copy that counting approach.

## Live audit corrections and scoped smoke

Read the official Decisions, Models, Models API and Authentication Markdown pages
linked by `https://docs.subq.ai/llms.txt`, including the Decisions errors section.
The top-level `/errors` page does not exist; Decisions documents 402 as insufficient
credits and 422 as an invalid request or non-decision model.

The response `x-request-id` header is now the sole ID source, validated with
`SafeToken`; body IDs are ignored. Every attempt records only method, path, status,
latency and sanitized ID in `route.attempts` and logs, including non-200 attempts
and timeouts (null status if no response). Bodies and other headers are not recorded.
`probe_models()` returns structured sanitized metadata plus `model_visible`;
`check_model_visibility()` remains its bool wrapper. GETs do not spend decisions.

402 uses `provider_unavailable` and is not retried. 422 remains `invalid_response`
and is not retried. Cross-owner request: add dedicated `insufficient_credits` and
`invalid_request` RouteReasonCode values; neither current enum describes those
cases exactly. Frozen contracts were not changed. Retry-After is deliberately
ignored (no sleep), retaining one transient retry and three requests per run,
further limited by the configured budget and caller's remaining allowance.

`pool_evidence(bank, choice, used_input_sha256=())` delegates to the shared
`CandidateBank.pool_for` and `pool_sha256`; it returns choice and chosen/alternative
pool hashes/sizes without inputs. It is a small operator helper, not a replacement
worker selector. The smoke uses a small synthetic public bank; its pool evidence
does not claim to describe a production worker's actual bank. Tests also prove all
three live-shaped choices produce distinct pools in the pinned seed17 bank.

Operator command (not run live during development):

```sh
uv run --locked python -m proofhunter.snap --live --decisions 3 --output snap-evidence.json
```

Set `SUBQ_API_KEY` in the environment first. No full live settings or unrelated
credentials are needed. Without `--live` the command refuses before any I/O.
`--decisions` accepts 1–3 and caps actual POST attempts, including retries. Exactly
one GET precedes decisions. States vary on steps 1–2 and offer two or three
strategies. Evidence includes UTC, sanitized per-attempt metadata, routing output,
pool summaries, and model/URL/timeout/limit plus their SHA256 prefix. It excludes
the key, headers and provider bodies. Account entitlement/real model behavior and
real worker pool effects remain live-unverified. FixtureRoute is unchanged.

Initial offline setup lacked cached grpcio; `uv sync --locked` installed the exact
locked environment without changing dependencies or lockfile.

Final validation (all commands prefixed `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline`):

```text
uv run --locked pytest -q
915 passed, 1 warning in 13.67s
uv run --locked ruff check .
All checks passed!
uv run --locked mypy src bench tools tests/integration
Success: no issues found in 72 source files
uv run --locked python tools/gate.py setup
[gate] setup PASSED
uv run --locked python tools/gate.py integrated
[gate] integrated PASSED
```

The pytest warning is the locked Starlette/FastAPI integration's
`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated;
install httpx2 instead.` The installed environment matches the lock; dependencies
remain unchanged. No visual UI check applies to this adapter/CLI work.

## Remaining live gate

PRD section 3 still requires an operator-authorized check of real account model
visibility, an actual parsed Snap choice, latency and account limits, and proof
that the live choice changes the candidate batch. Record sanitized request ID,
UTC timestamp, status and configuration fingerprint in `docs/provider-smoke.md`
when that gate is run. Mocked live-mode decisions here prove local adapter and
shared-pool behavior only, not provider entitlement or live API compatibility.
