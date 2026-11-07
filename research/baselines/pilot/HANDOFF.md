# Baselines lane handoff (research-protocol-v1)

- Setup SHA (branched on): `cadc5b4d91828fec1adcb5341a11c2c466e57c1f` (schemas/interfaces from `13b96671`)
- Branch: `devin/1794090000-research/baselines` (no PR)

## Owned files
- `research/baselines/{__init__,selectors,meta,frontier,grid,ledger,config,smoke}.py`, `research/baselines/models.json`
- `tests/research/baselines/{__init__,conftest,test_selectors,test_providers,test_grid}.py`
- `research/baselines/pilot/` committed copies of the pilot reports and ledger (`research/artifacts/` is gitignored by setup)

## Arms
Deterministic: `fixed_order`, `uniform_random` (seeded), `simple_heuristic`, `builtin_eig` (production
`proofhunter.bayes` read-only) via `selectors.make_selector(arm_id, *, seed)`.

Model x role grid (`grid.py`), 12 arms named `<role>:<model>`, models `gpt-6.1-sol`, `gpt-6-astra` (OpenAI Responses API)
and `muse-spark-1.3`, `muse-wonder-1.0` (Meta chat completions). No cell is skipped (`models.json` `grid.skipped` is empty).
- `generator:<model>`: the production plan prompt (`proofhunter.meta.build_request`) once, then the production plan validation,
  then the built-in EIG selector picks. Meta uses the production adapter itself (`meta.meta_generator`); OpenAI uses
  `frontier.ResponsesGenerator` with the same prompt, the same JSON schema (strict) and the production validator. Fallback: the built-in plan.
  Both use 2 attempts (the production adapter's count).
- `reviewer:<model>`: one call ranks up to 6 pool ids (strict JSON schema), with no feedback. Fallback: fixed order.
- `agent:<model>`: single strict `run_pair(candidate_id)` tool restricted to the pool, the same `PairFeedback`, the eligible
  (coverage-guarded) ids each turn, and the harness 6-pair budget. Fallback: fixed order.
- The reviewer and agent prompts, schemas, caps and retry policy (3 attempts on 429/5xx/transport/timeout, 90 s timeout) are
  identical for both providers; only the client (`ResponsesClient` / `ChatClient`) differs.
- Meta client: research-side `frontier.ChatClient`, posting to the production endpoint `META_CHAT_COMPLETIONS_URL` with the
  production settings (`MAX_COMPLETION_TOKENS`=4096, `REASONING_EFFORT`=low, temperature 0). `src/` is unchanged. Snap is never called.
- Factories: `grid.make_generator(arm, **kw)`, `grid.make_arm_selector(arm, *, seed, **kw)`, `grid.grid_arms()`. Old ids are kept as
  aliases (`meta_muse_spark_eig` → `generator:muse-spark-1.3`, `frontier_reviewer` → `reviewer:gpt-6.1-sol`, and so on).
- Degraded rows (coordinator decision) count as the arm's own results, with the fallback outcome and all provider cost incurred.
  The measurement lane reports the degraded rate per arm by `degraded` reason.

### Provider difference: Meta tool_choice
The Meta API rejects forced tool choice. Probed live on 2026-11-07, `"required"`, `"none"` and a named function all return
HTTP 400 (`only "auto" is supported for tool_choice`). So the Meta agent sends `tool_choice="auto"` and relies on the shared
prompt, while OpenAI forces `run_pair`. Native tool calls work on both Meta models. A Meta turn without a tool call
degrades with `malformed_output`, the same fallback OpenAI uses. `parallel_tool_calls` is not sent to Meta. If the model
returns several calls, only the first valid one runs; the others get an error result (same code path for both providers).

## Pricing (`models.json`)
- gpt-6.1-sol: $2 in / $0.10 cached / $10 out per 1M tokens. gpt-6-astra: $10 / $1 / $50. Both verified 2026-11-07 at
  https://developers.openai.com/api/docs/pricing, so these rows are `measured`.
- muse-spark-1.3: $1.25 / $0.15 / $4.25. muse-wonder-1.0: $4.00 / $0.20 / $20.00. Source: "dev.meta.ai/docs/models, as
  provided by user on 2026-11-07, pending page verification". Meta rows are `estimated` even when the response reports
  token usage, until the page is verified.

## Live grid smoke (pilot, reviewed demo cases only)
`python -m research.baselines.smoke --live --max-cases 3 --tag=_grid` → `pilot/smoke-live_grid.json`
(cases: buggy, half_even and fixed at seed 1633385816). Latency is the summed provider wall time per case.

| arm | cost USD per case (buggy / half_even / fixed) | arm total | provider latency s per case | calls | pairs | degraded |
|---|---|---|---|---|---|---|
| `generator:gpt-6.1-sol` | 0.0032 / 0.0033 / 0.0023 | 0.0088 | 2.3 / 2.4 / 1.5 | 1 / 1 / 1 | 1 / 1 / 6 | none |
| `generator:gpt-6-astra` | 0.0160 / 0.0189 / 0.0115 | 0.0464 | 3.2 / 3.3 / 1.4 | 1 / 1 / 1 | 1 / 1 / 6 | none |
| `generator:muse-spark-1.3` | 0.0057 / 0.0082 / 0.0081 | 0.0221 | 12.3 / 14.7 / 16.8 | 1 / 1 / 1 | 1 / 1 / 6 | none |
| `generator:muse-wonder-1.0` | 0.0328 / 0.0312 / 0.0377 | 0.1016 | 12.2 / 13.0 / 15.6 | 1 / 1 / 1 | 1 / 1 / 6 | none |
| `reviewer:gpt-6.1-sol` | 0.0098 / 0.0091 / 0.0082 | 0.0270 | 4.3 / 2.4 / 1.5 | 1 / 1 / 1 | 1 / 1 / 6 | fixed: empty_ranking |
| `reviewer:gpt-6-astra` | 0.0411 / 0.0438 / 0.0408 | 0.1258 | 1.7 / 2.3 / 2.1 | 1 / 1 / 1 | 1 / 1 / 6 | fixed: empty_ranking |
| `reviewer:muse-spark-1.3` | 0.0126 / 0.0098 / 0.0110 | 0.0334 | 12.4 / 10.3 / 11.2 | 1 / 1 / 1 | 1 / 1 / 6 | fixed: ranking_exhausted |
| `reviewer:muse-wonder-1.0` | 0.0354 / 0.0284 / 0.0318 | 0.0956 | 5.7 / 5.5 / 7.4 | 1 / 1 / 1 | 1 / 1 / 6 | fixed: ranking_exhausted |
| `agent:gpt-6.1-sol` | 0.0084 / 0.0085 / 0.0584 | 0.0753 | 1.7 / 1.4 / 9.2 | 1 / 1 / 6 | 1 / 1 / 6 | none |
| `agent:gpt-6-astra` | 0.0419 / 0.0423 / 0.2876 | 0.3719 | 1.1 / 1.2 / 8.1 | 1 / 1 / 6 | 1 / 1 / 6 | none |
| `agent:muse-spark-1.3` | 0.0067 / 0.0115 / 0.0498 | 0.0680 | 3.3 / 8.5 / 26.3 | 1 / 1 / 6 | 1 / 1 / 6 | none |
| `agent:muse-wonder-1.0` | 0.0215 / 0.0227 / 0.1401 | 0.1843 | 2.4 / 3.6 / 20.7 | 1 / 1 / 6 | 1 / 1 / 6 | none |

Grid total: **$1.160** = $0.655 OpenAI (measured) + $0.505 Meta (estimated). The no-defect `fixed` case: reviewers
rank fewer than 6 valid ids (OpenAI: an empty ranking; Meta: a short ranking), then fall back to fixed order.

Probe before the grid smoke (`pilot/smoke-live_probe.json`, plus 4 direct Meta tool_choice probes): $0.0141 in total. The 4 Meta
HTTP 400 rows (forced tool_choice) have cost `unknown`.

Ledger (`pilot/ledger.jsonl`, 132 entries, all pilot/live): OpenAI $0.761 measured (40) + $0.321 estimated (40; historical
gpt-6-sol rows and one exploratory call). Meta: $0.913 estimated (48), plus 4 rows with unknown cost (HTTP 400). No job estimate was above $2.

## Tests (all pass)
```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research   # 143 passed
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q                                      # 1320 passed
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check .                                    # clean
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration          # clean
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked mypy research/baselines      # clean
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python tools/gate.py setup|integrated          # PASSED
```
MockTransport tests cover:
- both providers: 429/503 retries, timeout, non-JSON body, bad envelope, malformed output, unknown ids, invalid
  tool-call cap, text instead of a tool call, missing key;
- Meta only: `finish_reason` length/content_filter;
- the OpenAI generator: 500 and an invalid plan, which fall back to the built-in plan;
- the grid: 12 stable names and the provider of each cell;
- prompt parity between Meta and OpenAI reviewers.

## Known limitations
- The smoke cases are easy demo cases (most arms find the bug at pair 1). They are not evidence of arm quality.
- Meta prices are unverified (estimated). Meta's agent cannot force tool choice (see above).
- `frontier_agent_free_input` is out of scope for v1 and raises NotImplementedError.
- `smoke.py` is a local stand-in for the measurement lane's `EpisodeRunner` and has no confirmation step. `JsonlLedger` is a local helper.

## Dependencies
- Dataset lane: validation cases.
- Measurement lane: `EpisodeRunner`, the shared ledger, and the degraded-row rule.
