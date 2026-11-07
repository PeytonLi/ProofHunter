# ProofHunter setup freeze (G0)

This document is the handoff for the nine parallel roles. Everything listed here is setup-owned and frozen. Do not edit setup paths from a role branch; record any needed contract change in your handoff and return `BLOCKED` so the coordinator can cut a new freeze.

## Versions

| Item | Pin |
|---|---|
| Python | 3.12 (`.python-version`, `requires-python >=3.12,<3.13`) |
| uv | 0.13.7 used to produce `uv.lock` |
| pydantic | 2.14.3 |
| fastapi / uvicorn | 0.162.2 / 0.54.0 |
| httpx | 0.28.1 (mock with respx 0.23.1) |
| pyjwt[crypto] | 2.15.1 (GitHub App JWT) |
| uagents / uagents-core | 0.26.1 / 0.4.12 |
| daytona | 0.231.0, stable v0 client only (`daytona.v2` alpha is out of scope) |
| pytest / pytest-asyncio | 9.2.0 / 1.4.1.post1 (`asyncio_mode = "auto"`) |
| ruff / mypy | 0.17.4 / 2.4.1 (strict, pydantic plugin) |

Install with `uv sync --locked`, then run everything with `UV_OFFLINE=1 uv run --locked ...`. Do not add dependencies; the lock is frozen. If you need one, report it as a setup defect.

## Mode boundaries

- `PROOFHUNTER_PROFILE=offline` is the default and the only profile used in automation. `proofhunter.config.load_settings` never reads provider credentials in this profile and raises `ConfigError` if any `PROOFHUNTER_ENABLE_*` live flag is true.
- `live` requires the service token, a different operator token, `MODEL_API_KEY`, `SUBQ_API_KEY`, `GITHUB_APP_ID` and `GITHUB_PRIVATE_KEY_PATH`; the live runner additionally needs `DAYTONA_API_KEY`, `DAYTONA_SNAPSHOT`, `DAYTONA_IMAGE_DIGEST`; webhooks need `GITHUB_WEBHOOK_SECRET`.
- Provider origins are constants in `proofhunter.config` (`META_CHAT_COMPLETIONS_URL`, `SUBQ_DECISIONS_URL`, `SUBQ_MODELS_URL`, `GITHUB_API_ORIGIN`). They are not configurable and never user-supplied.
- Live features default off: `PROOFHUNTER_ENABLE_LIVE_RUNNER`, `PROOFHUNTER_ENABLE_GITHUB_WEBHOOK`, `PROOFHUNTER_ENABLE_GITHUB_CHECKS`. The API binds to `127.0.0.1` by default.
- Offline results are labeled `offline_fixture` everywhere. `ReceiptV1` rejects offline receipts that claim live provider modes or non-fixture observations, and `CheckIntent` only accepts `execution_mode="live"`, so an offline result can never become a GitHub check.
- Configuration ownership follows PRD section 9: ACP gets only `PROOFHUNTER_API_TOKEN` and `PROOFHUNTER_API_BASE`; model keys stay in the worker; Daytona keys in the sandbox worker; GitHub keys in the GitHub worker and webhook handler.

## Import paths

```python
from proofhunter.contracts import ReceiptV1, ProbeRecord, canonical_json, ...  # DTOs and serializer
from proofhunter.ports import SourcePort, MetaPort, RoutePort, RunnerPort, CheckPort, RunStore, ...
from proofhunter.config import Settings, load_settings, load_enrollments
from fixtures.adapters import FixtureSource, FixtureMeta, FixtureRoute, FixtureRunner, ...
from fixtures import catalog, reference
```

`fixtures/` and the repo root are importable in tests (`pythonpath = ["src", "."]`) and in the editable install. Production code under `src/` must not import `fixtures.reference`; only the integration offline profile may bind `fixtures.adapters`.

Tests run with `--import-mode=importlib`, so test file names may repeat across `tests/<role>/` without `__init__.py`.

## Ports and required exports

All signatures are in `src/proofhunter/ports.py`. External I/O is async; math, oracle and SQLite persistence are sync.

| Owner | Module | Must export | Port or function |
|---|---|---|---|
| github | `proofhunter.github` | `create_source_port(settings, enrollments)`, `create_check_port(settings)`, `create_repository_validator(settings, enrollments)`, `create_webhook_verifier(settings)` | `SourcePort`, `CheckPort`, `RepositoryValidator`, `WebhookVerifier` |
| meta | `proofhunter.meta` | `create_meta_port(settings)` | `MetaPort.propose(context) -> HypothesisPlan` |
| snap | `proofhunter.snap` | `create_route_port(settings)` | `RoutePort.choose(state, eligible) -> RouteDecision` |
| bayes | `proofhunter.bayes` | `initialize`, `generate`, `select`, `update` | `Initializer`, `Generator`, `Selector`, `Updater` |
| runner | `proofhunter.runner` | `create_runner_port(settings)` | `RunnerPort.attest()`, `RunnerPort.observe(request)` |
| oracle | `proofhunter.oracle` | `evaluate`, `classify`, `compute_coverage`, `seal`, `verify_receipt`, `validate_replay` | `Evaluator`, `Classifier`, `CoverageFn`, `Sealer`, `ReceiptVerifier`, `ReplayValidator` |
| acp | `proofhunter.acp`, `proofhunter.acp.__main__` | `create_control_client(settings)`, `parse_command` | `ControlApiClient` |
| store | `proofhunter.store`, `proofhunter.api` | `create_store(settings)`, `create_app(deps: ApiDependencies)` | `RunStore`, `ControlStore` |
| bench | `bench.run` | `main` | `python -m bench.run --offline --split sample ...` |
| integration | `proofhunter.pipeline`, `worker`, `cli`, `__main__` | `cli.main` | `VerificationWorker` |

`REQUIRED_IMPLEMENTATIONS` in `ports.py` is the machine-readable form of this table; `tools/gate.py integrated` fails if any module or callable is missing. Path ownership is `MODULE_OWNERS` in the same file (each role also owns `docs/agent-handoffs/<role>.md`).

Failure signaling: raise `AdapterError(provider, ReasonCode, sanitized_message, retryable=...)`. Never put provider bodies, headers, tokens or source text in the message. `RoutePort` never raises for provider problems; it returns `fallback` (order `constraint`, `boundary`, `seeded_fuzz`), `policy` with `call_budget`, or `single_eligible`. `RunnerPort.observe` never raises for target misbehavior; it returns an explicit unusable `outcome`. `RunStore` writes raise `LeaseLostError` when the owner or epoch no longer matches; `ControlStore.create_run` raises `IdempotencyConflictError` on a changed body under the same key.

## Contract rules (contracts.py)

- `schema_version = "1.0"`. Every model is frozen and forbids extra keys. Integers are strict (booleans, floats and numeric strings are rejected). Nullable fields are required and must be sent as explicit `null` unless a default is shown.
- Timestamps are UTC and serialize as `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Run and attempt IDs are lowercase UUIDv4. SHAs are lowercase hex; image digests are `sha256:<64 hex>`.
- Floats appear only as measurements (probabilities, likelihoods, EIG, confidence). They must be finite and are rounded to 12 decimals on validation. Model vectors are objects keyed by canonical hypothesis ID (`H0`, `per_item_rounding`, `half_even_rounding`, `other`).
- Public projections: `PublicRevision` (no `installation_id`), `PublicObservation` (no `sandbox_id`), `PublicRouteDecision` (no `provider_request_id`), `PublicProbeRecord`. `ReceiptV1` is built only from public types; use `public_revision`, `public_observation`, `public_route`, `public_probe`.
- `HypothesisPlanPayload` is the exact Meta structured-output shape; `HypothesisPlan` adds `provenance`. The built-in plan is `builtin_hypothesis_payload()`.
- `Replay.command` must come from `build_replay_command(execution_mode, run_id, probe)` and is checked by regex.
- `CandidateBank.pool_for(strategy, used)` returns unseen candidates of a strategy plus unused anchors; `eligible_strategies(used)` lists strategies with an unseen non-anchor candidate.

## Canonical serializer (`ph-canonical-json-v1`)

`canonical_json(value)` is `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` encoded as UTF-8, applied to `model.model_dump(mode="json")`. Keys sort by code point and array order is preserved. Floats use Python's shortest round-trip `repr`. It raises `CanonicalEncodingError` for NaN or Infinity, floats not rounded to 12 decimals, integers outside signed 64 bits, non-string keys, unsupported types and lone surrogates.

- `input_sha256(x)` hashes `{"discount_percent": d, "prices_cents": [...]}`. The sentinel `([15,15,15], 10)` hashes to `326b2a8732d3411ecd4165eeab7faf0abb8efc089d0b3f333c5ec1264635635a`.
- `pool_sha256(candidates)` hashes the sorted list of input hashes.
- `compute_receipt_sha256(receipt)` hashes the receipt with `receipt_sha256` removed. `verify_receipt_sha256(json)` never raises.
- `enrollment_fingerprint(enrollment)` hashes the enrollment without `installation_id`.
- The golden vector is in `tests/contracts/test_canonical.py` (expected SHA-256 `cbee24ee7c63144490c513997aa81df7b9de928783973b4bec5349f9018cc6da`).

## Fixtures

| Path | What it is |
|---|---|
| `fixtures/demo/contract.json` | Trusted `checkout-rounding@1` contract (its SHA-256 is `contract_sha256`) |
| `fixtures/demo/revisions/{base,fixed,buggy,half_even}/checkout.py` | Reviewed demo revisions: base and fixed return 41 on the sentinel, buggy 42, half-even 40 |
| `fixtures/demo/harness.json` | Offline fixture harness descriptor (its SHA-256 is `harness_sha256`) |
| `fixtures/demo/manifest.json` | Fixture commit SHAs, blob SHAs, source hashes and PR cases `buggy` (#1), `fixed` (#2), `half_even` (#3), `existing` (#4) |
| `fixtures/demo/candidate_bank.seed17.json` | Pinned 63-candidate bank; the sentinel is the unique highest-EIG input at step one |
| `fixtures/demo/receipts/{buggy,fixed}.seed17.json` | Golden sealed offline receipts |
| `fixtures/providers/*.json` | Wire examples: Meta request/response and plan payload, Snap request/response/models, GitHub pull/contents/tree |
| `config/enrollments.example.json` | Example enrollment for the fixture demo repo `proofhunter-demo/checkout-demo` |
| `schemas/*.schema.json`, `schemas/samples/*.json` | Frozen JSON Schema snapshots and one sample per boundary model |

Fixture adapters in `fixtures/adapters.py`:

- `FixtureSource`: `SourcePort` over the demo PR cases; `head_overrides` simulates a moving head.
- `FixtureMeta`: returns the recorded plan; `fail_with=ReasonCode...` raises `AdapterError`.
- `FixtureRoute`: scripted `stub` choices for two steps, then `policy`/`call_budget`.
- `FixtureRunner`: runs only the reviewed revisions, chosen by exact source SHA-256 (unknown bytes are `denied`). `faults={(Side, input_sha256 or None): ObservationOutcome | int}` injects timeouts, invalid output or a flaky value. `isolation=` simulates a failed attestation.
- `FixtureChecks`: never publishes (`skipped_offline`).
- `FixtureRepositoryValidator`, `FixtureWebhookVerifier`: demo-only URL binding and HMAC (not hardened).
- `InMemoryRunStore`: `RunStore` and `ControlStore` with idempotency, quotas and lease fencing.
- `FixtureControlApi`: `ControlApiClient` bound to a store.
- `SteppingClock`, `SequentialIds`: deterministic clock and UUIDv4s.

`fixtures.reference` holds the reference predictors, `rounding-likelihood-v1`, EIG, update, oracle, coverage and verdict rules used by the fixtures. `fixtures.simulate.simulate_case(name, seed, runner=..., meta=..., route=...)` runs a reference offline case over the fixture ports. Owners can use these as adjacent-boundary stand-ins and as cross-checks. They are not production code.

## Gates

- `python tools/gate.py setup` checks the lock (`uv lock --check`, `uv sync --locked --check`), fixture and schema drift (`python -m fixtures.build --check`), runs `pytest tests/contracts`, ruff on setup paths and mypy strict on the shared modules and fixtures.
- `python tools/gate.py integrated` checks `REQUIRED_IMPLEMENTATIONS`, reruns the setup gate, runs the full `pytest -q`, both demo cases, counterexample replay, refusal of a tampered receipt and the sample bench, with all outputs in a temporary directory. It fails until every role module exists.
- Both strip provider credentials from the environment and force `PROOFHUNTER_PROFILE=offline`.
