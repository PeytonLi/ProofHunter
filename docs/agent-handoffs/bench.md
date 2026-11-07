VERDICT: PASS

# Production-scored offline benchmark handoff

## Base and scope

Started the isolated `devin/1794078900-proofhunter/bench-production` branch at
exactly `e504f140e20eb74ad7f2847206ad4b289a6e5482`, after fetching origin and
reading the entire root PRD and current `docs/setup.md`. Changes are confined to
`bench/`, `tests/bench/` and this handoff. Shared contracts, schemas, setup
fixtures, production modules and `uv.lock` are unchanged.

Offline only: no provider credentials read, provider calls, PR code execution,
deployment, agent publication, checks/webhooks, main merge or PR. Local target
execution is limited to reviewed first-party benchmark fixtures.

## Production scoring and retained controls

`bench.run.prepare` uses `proofhunter.bayes.generate`. `bench.arms.run_arm` calls
production `initialize/select/update` and production oracle
`evaluate/assess_probe/compute_coverage/classify`. No benchmark scoring imports
or calls `fixtures.reference`. Evidence binds requested input, base/head
commit/source, mode, image, snapshot and harness. Missing trusted oracle stops
before execution. Only usable matched-base evidence updates beliefs;
confirmation never creates a second update and shared defects are excluded.

Remaining fixture use: fixed recorded `FixtureMeta` plan shared across arms;
`FixtureRoute` stub choices then policy; deterministic IDs/clock/budget;
enrollment metadata; and `BenchRunner` fixture attestation/fault plumbing.
The local transport intersects source bytes with a separate reviewed hash
allowlist and checks exact bytes plus enrolled image/harness before execution.
A request/hash alone cannot enroll code. This is not isolation for arbitrary
source and injected timeouts are not OS-enforced. No live Meta/Snap claim.

All four arms remain: fixed anchor/boundary/constraint/fuzz/hash order, uniform
random without replacement, EIG over the full shared bank, and recorded
Meta/Snap/EIG over strategy-restricted pools from that bank. Fixed/random compute
production audit posteriors without adapting selection beliefs. Each run gets
six distinct pairs plus one fresh head replay (at most 13 invocations, within
the shared cap of 15). Clean verdicts require production coverage gates.

Master/bank seed 17 preregisters five replicate seeds:
`989287554, 181211149, 710491588, 1880716672, 1319078602`. All arms/cases get
paired seeds, bank and plan. Raw rows retain probes, coverage, failures,
exclusions, censored misses, invocation counts, local timings, replay command,
policy/implementation and source hashes. Fallbacks count once per decision.

## Explicit refreeze and corrected dev label

`bench/policy.json` freezes reviewed dev sources, manifest, seeds, budgets,
and scoring/transport plus shipped Bayes/oracle/shared-interface source hashes.
Evaluation refuses implementation, manifest, source-review or seed drift;
it never regenerates a policy automatically.

- Policy version: `bench-v2-production-anchor-boundary-constraint-fuzz-hash`
- Policy SHA: `472c181a8b4db9ab799a8c2a177401283b8bde8dec6626b55cb72165ac924c13`
- Dev manifest: `checkout-dev-v2`, 12 cases (9 defective, 3 clean), sample 6.
- Manifest SHA: `da58e11fe99e6dd2f841f5e71865fe5505a02b548b54698f5a4bb67c19d1f03a`

The old item-count case dropped a ninth item, outside the legal 0..8 domain:
its defective label was invalid. A legal seven-item off-by-one replaces it:
seven ones, 0% discount, expected 7/head 6. Its bytes, synthetic head revision
and manifest changed; all other dev source bytes remain. Witnesses now inherit
strict `CandidateInput` domain validation. The production generator was not
changed or tuned. EIG detects this legal case; fixed and recorded routing miss
it. Do not attribute v1/v2 metric differences solely to production scoring:
the generator implementation and invalid dev case both changed.

## Commands and results

`PROOFHUNTER_PROFILE=offline uv sync --locked` succeeded; only pinned dependency
downloads used network. Every check below used `UV_OFFLINE=1
PROOFHUNTER_PROFILE=offline` and `uv run --locked`.

| command after uv run --locked | result |
|---|---|
| `ruff check bench tests/bench` | PASS |
| `mypy bench tests/bench` | PASS, 35 source files |
| `pytest -q tests/bench tests/contracts` | PASS, 266 tests |
| `pytest -q tests/bayes tests/oracle` | PASS, 121 tests |
| `python -m bench.corpus --check` | PASS, 12 cases, manifest SHA above |
| `python -m bench.run --offline --split sample --seed 17 --output bench/results/sample.jsonl` | PASS, 120 runs and 5 separate fault scenarios |

Final combined regression command is `pytest -q tests/bench tests/contracts
tests/bayes tests/oracle`. No active Git pre-commit hook or pre-commit
configuration is present. Generated CSV now uses LF endings so normal Git
whitespace checks remain enabled; the policy and sample outputs were refrozen
after this format-only fix.

Runtime tests poison setup reference scoring functions and spy on every
production entrypoint while evaluating the sample. Assertions retain
41/41/42/42, shared-defect outcomes, honest misses, clean coverage, paired
budgets/seeds/bank/plan, normalized determinism and case-level bootstrap
weighting. They also check missing-oracle no-execution, source substitution,
unreviewed hash refusal, implementation/policy drift and seed refusal. No
holdout-labeled corpus is manufactured even for refusal tests.

The initial migration had one stale assertion that all item-count runs miss;
that is false with the legal v2 case and production bank. It was replaced by
actual censoring/coverage accounting, not forced outcomes. Initial lint line
length issues were fixed. Requirements and gates were not weakened.

## Frozen sample results

`bench/results/sample.jsonl`, `.csv`, `.summary.md`, `.adversarial.jsonl` are
committed evidence. The generated summary retains recall@1..6, medians,
inconclusive reasons, invocation counts, local p50/p95 and paired case CIs.

| arm | recall@3 | recall@6 | clean confirmed FP/runs | censored defective runs | base/head/confirm |
|---|---|---|---|---|---|
| fixed_boundary | .000 | .750 | 0/10 | 5/20 | 150/150/15 |
| random | .550 | .650 | 0/10 | 7/20 | 133/133/13 |
| eig_deterministic | 1.000 | 1.000 | 0/10 | 0/20 | 90/90/20 |
| meta_snap_eig | .750 | .750 | 0/10 | 5/20 | 105/105/15 |

Random has 14 coverage-insufficient inconclusives. Fixed/recorded routing miss
item-count despite bounded-clean verdicts; these remain censored detection
misses in recall denominators. Recorded routing makes 45 stub plus 60 policy
decisions, no provider-error fallbacks. Live usage is zero; cloud cost is unknown.

Paired recall@3 differences (95% percentile bootstrap, 2000 resamples, seed
2000, defective **cases** as units, paired seed means): EIG-random .450
[.200,.800]; EIG-fixed 1.000 [1.000,1.000]; recorded-routing-EIG -.250
[-.750,.000]. Four defective sample cases include two in the per-line family.
The degenerate interval is descriptive, not proof of generalization or
superiority. Misses are right-censored at six, not assigned a seventh pair;
medians condition on detection. No Brier/reliability claim.

Faults preserve production reasons: timeout/invalid output yield
coverage-insufficient, flaky replay nondeterministic, denied unknown source
and unverified attestation isolation-unverified. All timings are local
offline-fixture timings, never cloud measurements. No full dev comparison ran.

## Independent verifier holdout recipe (not run here)

1. Freeze dev manifest/policy, scoring/generator sources, arms, seeds and budgets
   before holdout authoring. Do not tune using held-out labels or selections.
2. A separate verifier prepares/reviews first-party sources outside authoring
   worktrees, with `Manifest`/`Case` schema, all split `holdout`, sample empty,
   and at least 36 cases: 24 distinct defective head hashes and 12 clean variants.
   Group near-duplicates together; no dev template group, base or head hash may
   overlap. Author independently equivalent clean bases, not copied dev bases.
   Independently prepare legal-domain witnesses or clean review rationales.
3. Canonically freeze manifest SHA excluding its own hash. Record preparer,
   reviewer and all prior exposure, including dev-bank familiarity. Keep labels
   and witnesses out of prompts, generator tuning and authoring worktrees.
4. Review every source as safe bounded first-party fixture code, not PR code.
   Prepare an external `FrozenPolicy` JSON: preserve this policy's scoring source
   hashes, budgets, master/replicate seeds; replace manifest SHA and reviewed
   source hashes with the exact external corpus values. Record review rationale;
   set `reviewed_first_party=true` only after review. Compute policy SHA with
   `canonical_sha256(payload_without_policy_sha256)`. Self-hashing is not proof
   of code safety: independent review is mandatory before local execution.
5. Run outside this branch:

   ```sh
   UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked python -m bench.run \
     --offline --split holdout --seed 17 --corpus-dir /private/reviewed-corpus \
     --policy /private/reviewed-policy.json --output /private/holdout-results.jsonl
   ```

   Shipped dev policy cannot enroll holdout code. Policy/source review, manifest,
   source hashes, domain/split/composition and group/hash separation must pass
   before arm execution. Labels join only at scoring, never in `ArmInput`.
6. Publish raw rows, summary, frozen hashes and exposure notes. Report authorized
   live/provider measurements separately. This author did not create, inspect
   or run holdout labels or results.

## Remaining gates

No frozen-contract blocker or unresolved owner defect found. Independent
holdout, live providers/Daytona latency/cost, real ASI:One/Agentverse release,
and operator publication remain pending by instruction. No live-release claim.
