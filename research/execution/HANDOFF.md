# Integration lane handoff (LANE=integration)

Branch `devin/1794090000-research/integration`, rebased on setup `cadc5b4d91828fec1adcb5341a11c2c466e57c1f`
(schemas/interfaces from `13b96671`). No frozen file was edited: `src/`, `bench/`, `schemas/`, `fixtures/`,
root locks, `research/cli.py`, `research/schemas/**`, `research/spec/**` are byte-identical to setup.

## Owned files

- `research/execution/pool.py` - shared Track A pool via `proofhunter.bayes.generate` + builtin plan; checks protocol pool policy.
- `research/execution/sandbox.py` - reviewed-template-only executor (SHA-256 allowlist, SIGALRM deadline, int64/invalid/exception mapping) and `FaultInjectingExecutor`.
- `research/execution/runner.py` - `ResearchEpisodeRunner` (`I.EpisodeRunner`): 6 pairs, 1 confirmation, 2 retries, 15 invocations hard ceiling, production oracle/evidence/classify, strict `RawResultRow`, ledger-derived costs.
- `research/execution/lanes.py` - lazy resolution of sibling lane modules (`LaneMissing` if absent), frozen artifact hash checks, JSONL ledger fallback.
- `research/execution/pilot.py` - R1 pilot: source/witness/pool/EIG audits, per-arm metrics, projected full-run budget. Refuses the test split.
- `research/execution/commands.py` - `run_pilot`, `evaluate` (dispatched by the frozen `research/cli.py`).
- `research/execution/doubles.py` - offline self-test doubles over the reviewed bench corpus (NOT research data).
- `research/execution/reports/r1-doubles-pilot-report.{md,json}` - pilot self-test report.
- `tests/research/execution/test_runner.py`, `test_commands.py`.

## Merged lanes

`generator` @ `069c4a6`, `verification` @ `5367464`. Not yet pushed by their lanes: dataset, ranker, baselines,
measurement, execution. No conflicts so far.

## Commands and results

```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research   # 137 passed, 4 skipped (skips: other lanes' pending setup checks)
UV_OFFLINE=1 uv run --project research --locked ruff check research/execution tests/research/execution  # All checks passed
UV_OFFLINE=1 uv run --project research --locked mypy research/execution                               # no issues (8 files)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q                                      # 1278 passed, 2 skipped (pre-merge of generator/verification)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check .                                   # All checks passed
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration          # no issues (81 files)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked python -m research.cli run-pilot --doubles --seeds 5
```

R4 drills covered by tests: selector outage at reset and mid-episode, malformed selector choice, generator
outage/timeout/malformed output (fallback to builtin plan, event logged), transient and persistent timeout,
real deadline on a reviewed infinite loop, invalid output, flaky confirmation, confirmation timeout,
exact budget (all retries spent: 14 invocations = 2x6 + 2; 16th invocation refused), unreviewed source never
executed, unknown arm rejected, rows carry no label fields.

## R1 pilot status

Only the self-test ran: 12 reviewed bench cases x 3 arms x 5 seeds, offline. Witnesses reproduced 9/9, no defect
case without an exposing pool input, no review failures. Numbers in the report are integration evidence only,
not research results. The real pilot (smoke, then up to 50 validation cases) is blocked on the dataset lane
(`research.data.store.open_store`, `research.data.templates`) and baselines (`research.baselines.selectors.make_selector`):

```
uv run --project research --locked python -m research.cli run-pilot --store <dataset root> --smoke
uv run --project research --locked python -m research.cli run-pilot --store <dataset root> --cases 50 --seeds 5
```

## Artifact hashes (self-test run)

- rows.jsonl `7d61d999ff5ea0f3e28afe50b86e698edba5787f95d5f0711b0043ec41b81add` (contains wall-clock timings, so not byte-reproducible)
- audits.json `5d5041c33d2c79e97efa542740056497ba610d620c187da7c73bb29c50cba92b`
- No dataset or model artifacts exist yet on this branch.

## Costs

Measured: 0 USD. No paid call, no provisioned resource, ledger empty. Model-arm costs are reported as unknown
(never zero) until their lanes write ledger entries; the runner sums ledger entries appended during each episode.

## Known limitations

- `simple_heuristic`, ranker, frontier, Meta and Qwen arms run only once their lanes land; until then the pilot/evaluate skip them with the `LaneMissing` reason recorded.
- Real research cases report `template_registry_unavailable` until `research.data.templates` (`list_templates`, `render`) exists.
- The timeout uses `SIGALRM` (main thread, POSIX only). The executor runs reviewed templates in-process; it is not an isolation boundary and must never be pointed at untrusted code. Track B stays design-only.
- Pilot timings are offline in-process, not cloud/sandbox timings.

## Interfaces expected from other lanes (send-backs if they differ)

- dataset: `research.data.store.open_store(root) -> CaseStore`; `research.data.templates.list_templates()` items with `template_id/template_version/template_sha256`, `render(template_id, params) -> (base_bytes, head_bytes)`.
- baselines: `research.baselines.selectors.make_selector(arm_id, *, seed)`, `research.baselines.frontier.frontier_reviewer(model_id)`, `frontier_agent(model_id, *, free_input)`, `research.baselines.meta.meta_generator()`.
- ranker: `research.ranker.selector.load_ranker_selector(checkpoint, *, arm_id)`.
- generator: `research.generator.model.load_generator(kind, *, adapter, revision)`, `research.generator.prompt.build_input(sources)` (both already match).
- measurement: `research.metrics.ledger.open_ledger(path) -> Ledger` (JSONL fallback used meanwhile).
