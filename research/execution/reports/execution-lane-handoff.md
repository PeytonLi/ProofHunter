# Execution/Oracles lane handoff (for integration to merge)

Branch `devin/1794090000-research/execution`. Built on integration `026c580` (merged in), and through it on setup
`cadc5b4d91828fec1adcb5341a11c2c466e57c1f` (schemas/interfaces `13b96671`). I edited no frozen or production path:
`git diff --stat cadc5b4 HEAD` is empty for src, bench, schemas, fixtures, tools, config, root locks, `.github`,
research/schemas, research/spec, research/cli.py, OWNERSHIP.md and the research lock/pyproject.

## What integration should take (`git diff 026c580 HEAD`, 6 files)

| File | What it is |
|---|---|
| `research/execution/track_b.py` | Track B design-only adapter. `TrackBAdapter.plan(task, test_ids)` checks that the IDs are in the task's `candidate_tests` and returns `TrackBRefusal(executed=False, execution_status="blocked_no_qualified_sandbox", missing_prerequisites=...)`. `run()` always raises `TrackBExecutionBlocked`. `ISOLATION_PREREQUISITES` lists what must be qualified before execution is allowed: network egress denied, metadata endpoint denied, no credentials, pinned environment, fresh instance per run, CPU/memory/process/disk/wall limits, fixed runner command with allowlisted test IDs, cleanup evidence, reviewed qualification evidence. Enabling execution also needs a new protocol version. The module imports no process, network or container API. |
| `research/execution/process_sandbox.py` | `ProcessTemplateExecutor`, a drop-in `sandbox.Executor`. Every call is one counted attempt. The SHA-256 review gate and a static AST screen run first; on refusal it returns `denied` and starts no process. Otherwise the code runs in a fresh temp dir via `python3 -I -B -S` with an empty env and no credentials. Limits: rlimits for AS 256 MiB, CPU, NOFILE, NPROC, FSIZE=0 and CORE=0; a wall deadline at the protocol timeout; 4096 B output cap. It runs in `unshare --user --net` when the host supports it. The process group is killed and the temp dir deleted on every call. It returns `PhasedExecution`, which is `Execution` plus `startup_ms`, `execution_ms`, `cleanup_ms`, `cleaned` and `refusal`. |
| `research/execution/runner.py` (+17/-7) | Per-phase timing. `_Episode` sums `startup_ms` and `cleanup_ms` from a `PhasedExecution` into `Timings.sandbox_startup_ms` and `Timings.cleanup_ms`. Only the execution phase counts toward `execution_ms`/`confirmation_ms`. Executors that don't report phases leave startup/cleanup as `None`. A missing cleanup becomes `infra_error`, so it counts as failed. **`make_runner` now uses `ProcessTemplateExecutor`** instead of the in-process `ReviewedTemplateExecutor`, which has no rlimits, output bound or cleanup. If you don't want that, revert only that one line. The doubles still use the in-process executor. |
| `tests/research/execution/test_track_b.py` | Track B tests (5): run always refuses; frozen status literals; prerequisites listed; non-allowlisted test IDs rejected; no execution-capable imports. |
| `tests/research/execution/test_process_sandbox.py` | Process executor safety tests (22): unsafe sources refused by the screen; unreviewed source denied with `Popen` patched to fail; a runner with nothing reviewed counts every denied attempt as failed and returns `inconclusive`; all 12 reviewed bench sources pass the screen; phase timings and cleanup on a reviewed run; runner timings with the process executor (startup, execution, confirmation, cleanup); timeout; output bound; memory limit; no credentials and no file writes in the child; egress and metadata (169.254.169.254) denied inside the net namespace; fixed child program; `make_runner` uses the process executor. |
| `tests/research/execution/test_budget_safety.py` | Budget tests (80). Every one of the 13 frozen arms is run against 6 fault patterns (none, all fail, base timeouts, flaky head, confirmation fails, random) with adversarial selectors. Checked each time: ≤6 distinct pairs, ≤1 confirmation, ≤2 retries, ≤15 invocations; executor calls equal counted invocations; `failed` equals the number of non-OK attempts; rows validate. Also: the reserved confirmation still runs after both retries are spent, and the protocol ceiling equals 2*6+1+2=15. |

Not needed: my earlier standalone `allowlist.py`, `budget.py`, `pool.py`, `runner.py`, `commands.py` and `sandbox.py` (commits up to `3c21c0e`) were dropped in the merge in favour of integration's.

## Commands and results (at the handoff commit)

```
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research/execution/test_track_b.py tests/research/execution/test_process_sandbox.py tests/research/execution/test_budget_safety.py   # 107 passed
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research   # 244 passed, 4 skipped (other lanes' pending setup checks)
UV_OFFLINE=1 uv run --project research --locked ruff check research/execution tests/research/execution   # All checks passed
UV_OFFLINE=1 uv run --project research --locked mypy research/execution   # no issues (10 files)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q   # 1421 passed, 4 skipped
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked ruff check .   # All checks passed
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked mypy src bench tools tests/integration   # no issues (81 files)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python tools/gate.py setup        # PASSED
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python tools/gate.py integrated   # PASSED
```

## Hashes

- Reviewed bench source allowlist: 12 sources from `bench/policy.json`; canonical sha256 of the sorted digests is `b472277eb8a24ca474f13518b0d5c65bc214b8e2227d3e4a5d7cce15cfd442d4`.
- No dataset or model artifacts are produced by this lane. Run-row hashes come from integration's pilot.

## Costs

Measured 0 USD. No paid calls, no API keys used, no provisioned resources. No ledger rows were needed.

## Known limitations

- `ProcessTemplateExecutor` is a bounded local wrapper for reviewed first-party template code only. It is not a qualified untrusted-code sandbox, and Track B stays blocked.
- The net namespace needs unprivileged user namespaces. Without them the network tests skip and the child runs with no namespace. The other limits still apply.
- The AST screen is defence in depth on top of the hash gate. If dataset-lane templates use syntax outside the screen (e.g. `while`, attribute access), they will be `denied` until the screen or the templates are reviewed. This was checked against the 12 bench sources only, because `research.data.templates` hasn't landed yet.
- Process startup adds about 15-25 ms per invocation (at most 15 per episode).

## Remaining dependencies

Dataset lane (`research.data.templates`, `research.data.store`) for real Track A cases. The other arms depend on their own lanes, as listed in integration's HANDOFF.md.
