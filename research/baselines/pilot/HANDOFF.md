# Baselines lane handoff (research-protocol-v1)

- Setup SHA (branched on): `cadc5b4d91828fec1adcb5341a11c2c466e57c1f` (schemas/interfaces from `13b96671`)
- Branch: `devin/1794090000-research/baselines` (no PR)

## Owned files
- `research/baselines/{__init__,selectors,meta,frontier,ledger,config,smoke}.py`, `research/baselines/models.json`
- `tests/research/baselines/{__init__,conftest,test_selectors,test_providers}.py`
- `research/baselines/pilot/{HANDOFF.md,ledger.jsonl,smoke-live_v2.json}` (committed copies; `research/artifacts/` is gitignored by setup, full run outputs stay local)

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
- `models.json`: primary `gpt-6-sol`, optional `gpt-6-astra` (both listed by GET /v1/models on 2026-11-07), effort low,
  max_output_tokens 4096, store=false, retries 3 attempts on 429/5xx/transport/timeout (backoff 1 s, 2 s; Retry-After cap 8 s),
  90 s request timeout, 1 MiB response cap, per-episode caps (18 agent calls, 250k input / 40k output tokens).

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

## Live smoke (pilot, `python -m research.baselines.smoke --live`)
These are the 4 reviewed demo cases (buggy, half_even and fixed at seed 1633385816, plus buggy at seed 3469185555), not dataset validation cases,
because the dataset lane had not landed. Final run: `smoke-live_v2.json`.

| arm | per-case cost (USD, estimated) | provider latency |
|---|---|---|
| frontier_reviewer gpt-6-sol | 0.0024–0.0029 (cached prompt); 0.010 uncached | 1.2–3.0 s |
| frontier_agent gpt-6-sol | 0.0013 (1 pair) / 0.044 (6 pairs) | 0.9–8 s |
| meta_muse_spark_eig | unknown (no published price, no usage returned) | 7–37 s |

Ledger: 51 entries, all `phase=pilot`, `measurement_mode=live`: OpenAI 40 entries totalling **$0.321 estimated**. Meta has 11 entries
of HTTP attempts with **cost unknown**. Every pre-launch job estimate was ≤ $2. That includes a $0.005 upper-bound entry for one
exploratory shape-check call and 2 aggregate entries for 8 debug calls made before ledgering was wired.

## Known limitations
- gpt-6-sol does not appear in the public pricing table (2026-11-07). The assessment rate of $2 / $0.20 / $10 per 1M tokens is
  unverified, so its costs are marked `estimated`. gpt-6-astra and gpt-6.1-sol rates were verified on that page.
- `frontier_agent_free_input` raises NotImplementedError. The frozen `Selector.choose` must return an `input_sha256` from
  `state.available`, so free inputs need an interface change, which is the setup lane's call.
- The reviewer returns an empty ranking on the no-defect case, so the episode falls back to fixed order (`degraded=empty_ranking`).
- The smoke cases are easy: every arm except fixed order and random finds the bug at pair 1. This is not evidence of arm quality.
- `smoke.py` is a local stand-in episode loop, not the measurement lane's `EpisodeRunner`, and it runs no confirmation step.
- `JsonlLedger` is a local helper; switch to `research.metrics` when it lands.

## Dependencies
Dataset lane (validation cases), measurement lane (`EpisodeRunner`, shared ledger, how `degraded` is censored),
and the coordinator (verified gpt-6-sol price; a decision on the free-input interface).
