# Baselines lane handoff (research-protocol-v1)

- Setup SHA (branched on): `cadc5b4d91828fec1adcb5341a11c2c466e57c1f` (schemas/interfaces from `13b96671`)
- Branch: `devin/1794090000-research/baselines` (no PR)

## Owned files
- `research/baselines/{__init__,selectors,meta,frontier,ledger,config,smoke}.py`, `research/baselines/models.json`
- `tests/research/baselines/{__init__,conftest,test_selectors,test_providers}.py`
- `research/baselines/pilot/{HANDOFF.md,ledger.jsonl,smoke-live_v2.json,smoke-live_gpt61sol.json}` (committed copies; `research/artifacts/` is gitignored by setup, full run outputs stay local)

## Interfaces
- `selectors.make_selector(arm_id, *, seed)`: `fixed_order`, `uniform_random`, `simple_heuristic`, `builtin_eig`
  (also `meta_muse_spark_eig`, `qwen_*_eig` → the same production EIG selector; the plan comes from `EpisodeContext.plan`).
  `builtin_eig` calls `proofhunter.bayes.initialize/select/update` read-only and updates the belief from `PairFeedback`.
- `meta.meta_generator()`: production `proofhunter.meta.create_meta_port` (muse-spark-1.3, max_completion_tokens=4096,
  reasoning_effort=low, temperature 0, 2 attempts). Snap is never called. Any `MetaError` → `builtin_hypothesis_payload()`,
  with the reason kept on `.last`.
- `frontier.frontier_reviewer(model_id)`: one Responses call ranks ≤6 pool ids (strict JSON schema), no feedback.
  `frontier.frontier_agent(model_id)`: single strict tool `run_pair(candidate_id)`, forced tool choice, no parallel calls,
  the same `PairFeedback` as other arms, and the eligible (coverage-guarded) ids each turn. The harness keeps the paired-probe budget.
  Optional kwargs: `api_key, ledger, transport, phase, mode, run_id, estimated_job_usd`.
  Every provider failure → deterministic fixed order, plus a `selector.degraded` reason code. Rows should mark/censor on this.
- `models.json`: primary `gpt-6.1-sol` ($2 in / $0.10 cached / $10 out per 1M, verified 2026-11-07 at
  https://developers.openai.com/api/docs/pricing), optional `gpt-6-astra` ($10 / $1 / $50, verified). gpt-6-sol dropped
  (listed by /v1/models but unpriced). Effort low, max_output_tokens 4096, store=false, retries 3 attempts on
  429/5xx/transport/timeout (backoff 1 s, 2 s; Retry-After cap 8 s), 90 s request timeout, 1 MiB response cap,
  per-episode caps (18 agent calls, 250k input / 40k output tokens).
- Degraded episodes (coordinator decision): count as the arm's own results with the fallback outcome and all provider
  cost incurred; the measurement lane reports the degraded rate per arm by `degraded` reason.

## Tests (all pass)
```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research   # 123 passed
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q                                      # 1300 passed
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check .                                    # clean
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration          # clean
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked mypy research/baselines tests/research/baselines  # clean
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python tools/gate.py setup|integrated          # PASSED
```
Failure modes tested with httpx.MockTransport: 503/429 retries, 400 no-retry, timeout, connect error, non-JSON body,
bad envelope, malformed/non-schema output, unknown ids, empty ranking, incomplete status, refusal, missing key (no call),
invalid tool-call cap, text instead of a tool call. Meta: 503, malformed, timeout, missing key → built-in fallback.

## Live smoke (pilot, reviewed demo cases only)
Final run: `python -m research.baselines.smoke --live --max-cases 3 --tag=_gpt61sol` → `smoke-live_gpt61sol.json`
(cases: buggy, half_even, fixed at seed 1633385816). These are the reviewed fixture cases, not dataset validation cases.

| arm | per-case cost (USD, measured) | provider latency |
|---|---|---|
| frontier_reviewer gpt-6.1-sol | 0.0083–0.0100 | 1.7–4.3 s |
| frontier_agent gpt-6.1-sol | 0.0084–0.0085 (1 pair) / 0.058 (6 pairs) | 1.7–9.3 s |
| meta_muse_spark_eig | unknown (no published price, no usage returned) | 10–19 s |

gpt-6.1-sol reported 0 cached tokens on these calls. Earlier pilot runs used gpt-6-sol (`smoke-live_v2.json`).

Ledger (`ledger.jsonl`, 65 entries, all `phase=pilot`, `measurement_mode=live`):
- gpt-6.1-sol: 11 entries, **$0.1026 measured** (this smoke), plus 1 exploratory shape-check call ($0.005 upper bound, estimated).
- gpt-6-sol (dropped, historical): 39 entries, **$0.3159 estimated** (unverified rate).
- Meta: 14 HTTP attempts, **cost unknown**.
- OpenAI total: **$0.4235** across 51 entries. Every pre-launch job estimate was ≤ $2.

## Known limitations
- Historical gpt-6-sol pilot spend is priced at the unverified assessment rate and marked `estimated`.
- gpt-6-astra (optional arm) is pinned and priced but was not run live.
- `frontier_agent_free_input` is out of scope for v1 (coordinator decision) and raises NotImplementedError; the frozen
  `Selector.choose` can only return an `input_sha256` from `state.available`. The pool-restricted agent is the plan's arm.
- The reviewer returns an empty ranking on the no-defect case, so the episode falls back to fixed order (`degraded=empty_ranking`).
- The smoke cases are easy: every arm except fixed order and random finds the bug at pair 1. This is not evidence of arm quality.
- `smoke.py` is a local stand-in episode loop, not the measurement lane's `EpisodeRunner`, and it runs no confirmation step.
- `JsonlLedger` is a local helper; switch to `research.metrics` when it lands.

## Dependencies
Dataset lane (validation cases), measurement lane (`EpisodeRunner`, shared ledger),
which must apply the degraded-row rule above.
