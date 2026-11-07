VERDICT: PASS

# Offline benchmark handoff (G0)

## Built

- `bench/corpus/`: 12 reviewed first-party dev variants, three clean integer
  refactors, one shared/pre-existing defect, nine defective cases total. Each
  directory has exact base/head source bytes; most bases copy the correct fixture
  base. The versioned manifest freezes source/contract hashes, synthetic revision
  IDs, template groups, expected verdicts and independently hand-prepared witnesses
  or clean review rationales. `sample` explicitly lists six dev cases, including
  the item-count family that the frozen bank cannot reach. No holdout is present.
- `bench.harness.BenchRunner`: `RunnerPort`-compatible allowlisted local harness;
  request bytes must exactly match enrolled source hashes. Fresh in-process
  modules per invocation, real local durations, fault map, offline attestation.
  Unknown bytes are denied. This is **not** isolation for arbitrary untrusted code.
- `bench.arms`: label-free `ArmInput` accepts only source DTOs, enrollment, fixed
  recorded plan and precomputed bank. Four controlled policies, six distinct
  paired probes and at most one fresh head confirmation. Random samples uniformly
  without replacement; fixed order is anchors, boundary, constraint, seeded fuzz,
  with SHA ties. EIG uses all unused candidates; recorded Snap restricts its pool
  through `pool_for`. Only usable evidence updates adaptive beliefs. Baselines
  compute audit posteriors to satisfy `ProbeRecord` but never feed them forward.
- `bench.run`: required `main(argv: Sequence[str] | None = None) -> int` and CLI.
  JSONL has full probe DTOs, evidence/exclusions, separate invocation counts,
  right-censoring, replay command, hashes, versions and preregistered seeds. CSV,
  Markdown and a separate adversarial JSONL accompany it. Labels are joined only
  through `bench.scoring.score`, after arm execution.
- `bench.scoring`: recall at budgets 1..6, clean confirmed false positives,
  detected-only median with censored misses separate, inconclusive reasons,
  invocation counts and local p50/p95. Paired seed means and 2000 percentile
  bootstrap resamples **by defective case**, fixed bootstrap seed 2000.
- `bench.holdout`: external-only holdout validation, disjoint source hashes and
  template groups, minimum size/composition checks. Dev policies stay frozen.

## Commands and results

Ran `uv sync --locked` first (dependency network only); installation succeeded.
All commands below used `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline` and
`uv run --locked`. No provider was called and no API keys were read.

```text
pytest -q tests/bench
..........                                                               [100%]
10 passed in 2.24s

pytest -q tests/contracts
.............................................................            [100%]
205 passed in 1.69s

ruff check bench tests/bench
All checks passed!

mypy bench tests/bench
Success: no issues found in 34 source files

python -m bench.corpus --check
{"version": "checkout-dev-v1", "cases": 12, "manifest_sha256": "fe7283035d129331e74024d81942dcd6738ffb63ab832e8813716761d0cac77c"}
```

Initial checks surfaced mandatory route and posterior DTO requirements; these are now represented
without changing frozen contracts or adapting baseline beliefs.

Sample command:

```sh
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python -m bench.run \
  --offline --split sample --seed 17 --output /tmp/bench-results.jsonl
```

Produces 120 runs (6 cases x 4 arms x 5 seeds), plus five separate infrastructure
scenarios. Companion files: `/tmp/bench-results.csv`,
`/tmp/bench-results.summary.md`, `/tmp/bench-results.adversarial.jsonl`.
These requested temporary outputs are reproducible, not committed artifacts.
The five random seeds are deterministically preregistered by SHA-256 of the master
seed and replicate number; every row records the full list. Bank seed is always
the master seed (17 here), independent of replicate. All arms are paired on each
case and replicate. Deterministic arms repeat across replicates intentionally;
bootstrap units remain cases, not the repeated runs.

### Sample results (descriptive development results only)

| arm | recall@1..6 | clean FP/runs | median detected pairs | censored defects | inconclusive/runs | base/head/confirm | p50/p95 ms (offline_fixture) |
|---|---|---|---|---|---|---|---|
| fixed_boundary | .000, .000, .000, .750, .750, .750 | 0/10 | 4.0 | 5/20 | 0/30 | 150/150/15 | 2.485/2.914 |
| random | .100, .150, .300, .400, .400, .400 | 0/10 | 3.0 | 12/20 | 22/30 | 153/153/8 | 2.664/2.894 |
| eig_deterministic | .750, .750, .750, .750, .750, .750 | 0/10 | 1.0 | 5/20 | 0/30 | 105/105/15 | 2.900/4.880 |
| meta_snap_eig | .750, .750, .750, .750, .750, .750 | 0/10 | 1.0 | 5/20 | 0/30 | 105/105/15 | 2.319/3.874 |

Random had 22 coverage-insufficient inconclusives. Other arms had none, but all
arms missed item-count; a `no_defect_found` verdict on that defective case is still
a censored **detection miss**, never evidence of a clean label. Meta/Snap made 45
stub decisions and 60 policy/call-budget decisions; 0 provider-failure fallbacks
out of 105 decisions. No live calls/costs exist for these runs.

| paired contrast | detection@3 difference [95% case-bootstrap CI] | recall@6 difference [CI] |
|---|---|---|
| EIG - random seed mean | .450 [.150, .600] | .350 [.100, .550] |
| EIG - fixed | .750 [.250, 1.000] | .000 [.000, .000] |
| Meta/Snap/EIG - EIG | .000 [.000, .000] | .000 [.000, .000] |

Only four defective template cases are in this sample; two are in the same
per-line family. Intervals resample cases as specified, not template clusters;
neither independence across templates nor generalization is established.
No superiority claim is warranted.

### Separate adversarial suite

| scenario | verdict reason | observation failures |
|---|---|---|
| timeout_head | coverage_insufficient | timeout: 6 |
| invalid_output | coverage_insufficient | invalid: 6 |
| flaky_confirmation | nondeterministic | none; fresh value disagrees |
| isolation_unverified | isolation_unverified | no invocations |
| unknown_source | coverage_insufficient | denied: 12 |

The classifier's generic coverage verdict reason is preserved; transport-specific
reason codes remain separately visible in failure/exclusion counters and probes.

## Interface assumptions and production integration

G0 has no bayes/oracle/runner/meta/snap role modules. Bench intentionally imports
`fixtures.reference` and `fixtures.adapters` outside `src/`, through shared DTOs
and `RunnerPort`. Plans come from `FixtureMeta.propose(planning_context(...))`
once per case; both EIG arms receive that identical plan and bank. Recorded
source references must fit the supplied checkout file (the fixture plan refers
to line 4). FixtureRoute supplies `stub` decisions then `policy/call_budget`;
policy exhaustion is **not** a provider-error fallback.

`ProbeRecord` requires a route even in non-routing arms. Their DTO route is a
single-selected-strategy policy projection, **not** a restriction of the EIG
candidate pool; the arm and pool hash document actual selection. Fixed/random
singleton selector records are audit scores, not adaptive selection claims.
Replay commands use the shared `build_replay_command`; commands name synthetic
offline run IDs, not persisted production receipts. The benchmark CLI is the
reproducibility entrypoint; production replay storage is not implemented here.

Integration can substitute production initialize/generate/select/update and
evaluate/classify/compute_coverage functions with the same DTO signatures, and
inject a `RunnerPort` into `run_arm`. Re-freeze policies/results for any such
change. Do not replace this allowlisted local runner with arbitrary source exec.
Production provider modes/costs and Daytona timing must be reported separately.

## Independent verifier holdout recipe

1. Freeze this branch's dev manifest, policy source files, bank/generator version,
   budget and master seed **before** holdout authoring. Do not tune the bank.
2. A separate verifier, outside the authoring worktree, prepares a private corpus
   directory with `manifest.json` using the `bench.corpus.Manifest`/`Case` schema.
   Use split `holdout` on every case, stable IDs, version, source paths, exact
   SHA-256 hashes and synthetic 40-hex base/head revision IDs. Contract ID/hash
   must match the enrolled checkout-rounding contract. No labels enter prompts.
3. Prepare at least **36 cases**, including at least **24 distinct defective head
   hashes** and **12 clean variants**. Group near-duplicate edits under template
   groups; no template group or source hash may overlap dev/sample. This includes
   base hashes: **do not reuse the dev reference base bytes**. Write independently
   equivalent, reviewed clean bases for holdout, and fresh groups/implementations.
4. Independently hand-prepare each defective witness (prices, discount, expected
   half-up oracle value, head value) without reading candidate selections; verify
   head execution matches the recorded value and differs from the independent
   integer formula `(sum(prices)*(100-discount)+50)//100`. Clean variants require
   semantic review rationale. Review code as safe first-party implementations;
   the local transport cannot safely execute hostile code or enforce timeouts.
5. Set `sample` to an empty list. Compute `manifest_sha256` with
   `canonical_sha256(manifest.model_dump(exclude={"manifest_sha256"}))` and freeze
   the manifest and directory before evaluation. Document who prepared/reviewed
   them and a prior-exposure note for every evaluator, including any exposure to
   labels, witnesses, templates or this dev bank.
6. Run externally, keeping labels/witnesses out of the repo:

   ```sh
   UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python -m bench.run \
     --offline --split holdout --corpus-dir /absolute/private/corpus \
     --seed 17 --output /absolute/private/holdout-results.jsonl
   ```

   Validation refuses drift, overlapping hashes/groups, wrong splits and
   inadequate size/composition. Labels are joined only after arm execution.
7. Publish raw JSONL/CSV, companion summary, frozen hashes and prior-exposure
   note. Keep misses censored and conclusions descriptive. The verifier may
   separately publish the holdout after reporting; this branch contains none.

## Limitations

- Fixed bank was designed with knowledge of two rounding predictors; unmodeled
  families (notably item-count beyond eight) may be unreachable. No bank tuning
  was performed; misses remain in recall denominators.
- In-process fixture timings are local, exclude provider/network costs, and are
  never cloud execution timings. Fault timeouts are injected, not OS-enforced.
- No live provider or Daytona validation, independent holdout evaluation,
  Brier/reliability claims, UI/visual verification, commit or push was performed.
  This work is an offline benchmark, not a claim that the live release gate passes.
