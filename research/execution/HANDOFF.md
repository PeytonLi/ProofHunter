# Integration lane handoff (LANE=integration)

Branch `devin/1794090000-research/integration` (no PR, no merge to main). Setup: originally rebased on
`cadc5b4d91828fec1adcb5341a11c2c466e57c1f` (schemas/interfaces `13b96671`), then merged setup
`280f28cc0cca2449179501e0e0073cd63e617a82`. `git diff --stat 280f28c HEAD` is empty for `src/ bench/ schemas/
fixtures/ tools/ config/ pyproject.toml uv.lock .github/ research/cli.py research/schemas research/spec
research/OWNERSHIP.md research/pyproject.toml research/uv.lock`.

## Merged lanes (sibling-owned files always taken from the sibling on add/add conflicts)

| lane | SHA |
|---|---|
| setup | `280f28c` (on `cadc5b4`) |
| dataset | `b48a61f` -> `ac50b98` -> `c668241` (test-split leak repair; train/validation byte-identical) |
| measurement | `e125511`, `74fac5e` |
| ranker | `0f2cb1f` (fixture), `9e6a812` (real-v1) |
| baselines | `4c32c49`, `9bb5859` (gpt-6.1-sol primary) |
| generator | `069c4a6` |
| verification | `5367464`, `a058778`, `945f9a8` |
| execution | `ec947e1` reconciled into this layout (see below) |

## Owned files (`research/execution/**`, `tests/research/execution/**`)

- `pool.py` shared Track A pool: `bayes.generate(builtin_plan, CaseRecord.candidate_seed)`, protocol pool checks.
- `allowlist.py` reviewed-source allowlist (hash + rendered-template identity) and static AST screen.
- `isolation.py` one fresh `python -I -B -S` child per invocation: empty env, rlimits (AS 256 MiB, NOFILE,
  NPROC, FSIZE=0, CORE=0, CPU as a backstop 1 s behind the wall deadline), bounded output, process-group kill,
  temp-dir cleanup evidence, `unshare --user --net` when available, per-phase timings.
- `sandbox.py` `ProcessTemplateExecutor` (used by `make_runner`), in-process executor for doubles, `FaultInjectingExecutor`.
- `budget.py` exact episode budget (6 pairs, 1 reserved confirmation, 2 retries, 15 invocations).
- `track_b.py` Track B adapter: `plan()` returns `executed=False, execution_status="blocked_no_qualified_sandbox"`; `run()` always raises.
- `runner.py` `ResearchEpisodeRunner` (`I.EpisodeRunner`), oracle/evidence/classify, strict `RawResultRow`,
  phase timings, ledger-derived costs, add-on arms (`addons`: add-on id -> frozen base arm).
- `lanes.py` lane resolution, frozen artifact hash checks, paid-arm guard (`--mode live`, `--estimate ARM=USD`,
  refuses > 50 USD), add-on ranker checkpoints.
- `pilot.py` R1 pilot (refuses test): audits, per-arm metrics, phase timings, `degraded.jsonl`, projection.
- `commands.py` `run-pilot`, `evaluate` (dispatched by frozen `research/cli.py`); both write `degraded.jsonl`.
- `doubles.py` offline bench doubles (integration evidence only, not research data).
- `configs/pilot-ranker-real-v1.json` add-on arms `learned_ranker`, `learned_ranker_no_belief`.
- `reports/r1-validation/**` pilot reports, ledgers, degraded files, `report/` (measurement `research report`).
- tests: `test_runner.py`, `test_commands.py`, `test_sandbox_safety.py`, `test_budget_safety.py` (80, from
  execution lane), `test_track_b.py`, `test_ranker_coverage.py`.

Execution-lane reconciliation: its `process_sandbox.py` became `isolation.py` + `sandbox.ProcessTemplateExecutor`
(same limits, cleanup and timings); missing cleanup maps to `infra_error` as in its version. Its
`test_process_sandbox.py` targets its module layout and was not taken; the equivalent cases live in
`test_sandbox_safety.py`. `track_b.py`, `test_track_b.py`, `test_budget_safety.py` taken verbatim.
The AST screen was widened (owned file) because it refused about half of the dataset's reviewed templates: it
now admits `while`, keyword arguments, public attribute calls and top-level imports of `operator`, `math`,
`functools.reduce`, `fractions.Fraction`, `decimal` names only; dunder names/strings, private attributes,
nested or other imports remain refused. All 2,693 train+validation cases pass hash, render and screen.

## Commands and results

```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research   # 341 passed, 10 skipped
UV_OFFLINE=1 uv run --project research --locked ruff check research/execution tests/research/execution  # All checks passed
UV_OFFLINE=1 uv run --project research --locked mypy research/execution                               # no issues (12 files)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q                                      # 1494 passed, 13 skipped
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration          # no issues (81 files)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check .                                   # 6 errors, all in dataset-owned research/data/templates.py (send-back)
```

Pilot (dataset tarball extracted to `~/research-data`; Track A manifest `ba3d927d…1520`, train/validation
unchanged in `dcd14e3f…9340`):

```
uv run --project research --locked python -m research.cli run-pilot --store ~/research-data/track_a --smoke --arms fixed_order,uniform_random,simple_heuristic,builtin_eig
uv run --project research --locked python -m research.cli run-pilot --store ~/research-data/track_a --cases 50 --seeds 5 --arms fixed_order,uniform_random,simple_heuristic,builtin_eig --out ~/pilot-local
# live, keys bound per command (PROOFHUNTER_OPENAI_API_KEY, MODEL_API_KEY); one job per paid arm
uv run --project research --locked python -m research.cli run-pilot --store ~/research-data/track_a --cases 50 --seeds 5 --mode live --arms frontier_reviewer --estimate frontier_reviewer=5 --out ~/pilot-frontier_reviewer
uv run --project research --locked python -m research.cli run-pilot ... --arms frontier_agent --estimate frontier_agent=20
uv run --project research --locked python -m research.cli run-pilot ... --arms meta_muse_spark_eig --estimate meta_muse_spark_eig=1   # 1 = placeholder, price unpublished
uv run --project research --locked python -m research.cli run-pilot ... --arms learned_ranker,learned_ranker_no_belief --config research/execution/configs/pilot-ranker-real-v1.json
uv run --project research --locked python -m research.cli report --results <rows.jsonl...> --labels ~/research-data/track_a/labels/validation.json --ledger <ledger.jsonl...> --degraded <degraded.jsonl...> --out <dir>
```

## R1 validation pilot (50 validation cases x 5 seeds = 250 episodes per arm)

Cases: first 50 validation cases by sha256(case_id) (46 defect, 4 clean). Audits: 46/46 witnesses reproduced,
11 defect cases have no exposing input in the shared pool (recall ceiling ~0.76), no source-review failures.

PILOT_TABLE

Per-phase isolation overhead (local child process): startup ~17-26 ms, cleanup ~1.2-1.9 ms per episode-sum,
execution ~86-173 ms per episode. Not reverted.

## Projected full run (frozen test split, 650 cases after `c668241`, x 5 seeds)

PROJECTION

## Costs (all ledger rows in `reports/r1-validation/*-ledger.jsonl`)

COSTS

## Send-backs / known limitations

- baselines: `frontier_reviewer` raises `empty_ranking` on 20/50 cases (all 5 seeds each); the episode then
  stops with `no_defect_found` (75 missed defect episodes). Non-degraded reviewer episodes confirmed 150/150.
- baselines: Meta ledger rows carry `case_id=None` and no token usage; cost stays unknown.
- dataset: 6 ruff errors in `research/data/templates.py` (RUF005 x5, E501).
- ranker: logged-IPS checkpoints were re-featurized on `pool_seed(candidate_seed, seed)` pools; the runner (protocol)
  uses `candidate_seed` pools, matching dense_full. Ranker history-tag coverage counts mismatch tags; the oracle
  counts match only. They agree on every decision state (runner stops after a mismatch), tested on bench and
  validation pools.
- Grid arms `<role>:<model>`: `RawResultRow.arm_id` (Ident) accepts them, `ArmSpec.arm_id` (Slug) and frozen
  protocol-v1 do not. Add-on mechanism can run them on base specs once baselines exports a factory; adding them
  to the final evaluation needs a protocol version bump (setup lane).
- `isolation.py` is a bounded wrapper for reviewed first-party templates, not a qualified untrusted-code sandbox.
  Track B stays design-only. Test split never touched; evaluate on test waits for the verifier re-audit.
- Pilot latencies are local child-process timings plus provider wall time; not cloud sandbox timings.

## Remaining dependencies

Baselines grid SHA (muse-wonder-1.0, gpt-6-astra, role x model factory); reviewer empty-ranking fix; ranker v2;
generator (Qwen) frozen adapters for qwen_* arms; verifier sign-off on test split `c668241` before `evaluate --split test`.
