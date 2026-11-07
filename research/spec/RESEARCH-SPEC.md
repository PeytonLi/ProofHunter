# ProofHunter research protocol, v1 (`research-protocol-v1`)

Status: **frozen** at the R0 setup commit on `devin/1794090000-research/setup`. Changes need a
new protocol version issued by the setup lane. Machine-readable twins:
[`protocol-v1.json`](protocol-v1.json) (`ProtocolV1`) and
[`spend-policy.json`](spend-policy.json) (`SpendPolicy`). If prose and JSON disagree, the JSON
(validated by `research/schemas/models.py`) wins, and the setup lane fixes the prose.

## 1. Provenance

| Item | Value |
| --- | --- |
| Research starting SHA (R0 base) | `b97210ac37e9a352f7fcd5a3c4f013ab876b39a6` on `devin/1794078900-proofhunter-bootstrap` |
| Historical verified candidate SHA (ancestor) | `8dec16a0f7c8f5c0773a02c396f84c0bbf2de378` |
| Production contract | `checkout-rounding@1`, `checkout.total_cents(prices_cents, discount_percent)` |
| Production files frozen (sha256) | `pyproject.toml` `2952e1bf…cece8d`, `uv.lock` `34d1a6fc…0e0325`, `bench/policy.json` `e71b6269…3fe79d` (full digests in `protocol-v1.json`, enforced by `tests/research/test_setup_protocol.py`) |
| Research environment | separate uv project `research/pyproject.toml` + `research/uv.lock`, Python 3.12, linux x86_64 |

The production benchmark (`bench/corpus`, `bench/holdout`, `bench/policy.json`) is a separate
benchmark. The research track neither modifies it nor trains on it. Research cases must not share
source hashes with it.

## 2. Research questions

1. Does a learned LightGBM LambdaRank ranker find confirmed witnesses with fewer paired probes than
   fixed order, uniform random, a simple heuristic and the production EIG selector?
2. Does a QLoRA-tuned Qwen2.5-Coder-7B-Instruct hypothesis generator improve discovery over the
   untuned model and over the built-in plan?
3. Are ranker and generator effects additive (2x2 factorial)?
4. Do they beat strong frontier reviewer and tool-using agent baselines under the same budget?
5. Is any quality gain worth the added latency and cost?

## 3. Track A: supported checkout contract (primary deliverable)

### 3.1 Cases

- A case is one independently prepared `(base checkout.py, head checkout.py)` variant produced by
  a **reviewed, deterministic, parameterized template** owned by the dataset lane
  (`CaseRecord`: `template_id`, `template_version`, `template_sha256`, `params`,
  `params_sha256`). Only reviewed template code is executed.
- Kinds (`CaseLabel.kind`): `introduced_regression` (base correct, head defective),
  `existing_defect` (both defective, same value), `clean` (head correct; held-out clean controls).
- Labels are independent of every arm. A defect label needs a witness computed by the trusted
  oracle and reproduced by executing the reviewed template output
  (`reproduced_by = reviewed_template_execution`). Clean controls carry no witness and pass a
  reviewed equivalence check by the template author.
- Splits are grouped. `family_id`, `defect_origin`, `near_dup_cluster` and `template_id` each
  appear in exactly one split (`CaseManifest` validator). Test families are never seen in training
  or model selection.
- Targets (`split_targets`): train 2000 defect + 400 clean, validation 300 + 60, test 500 + 150,
  with at least 12/3/5 families. Final counts after filtering are reported, not the targets.
- Test labels are sealed (`LabelFile.sealed = true` for `test`) until final evaluation of frozen
  checkpoints.

### 3.2 Candidate, domain, pool

- Candidate = concrete input `{prices_cents: list[int], discount_percent: int}` in the production
  legal domain: at most 8 items, each 0..10000 cents, discount 0..100.
- Pool = `proofhunter.bayes.generate(plan, seed)` (generator `candidate-generator-v1`), at most
  64 candidates: 4 common anchors plus up to 20 per strategy (boundary, constraint,
  seeded_fuzz), deduplicated by `input_sha256`. The plan is the production built-in hypothesis plan;
  the seed is `CaseRecord.candidate_seed` combined with the replicate seed.
- **Every arm on a (case, seed) sees the identical pool** (`pool_sha256` recorded per row).
  Generator arms change the hypothesis plan the selector scores with (EIG/ranker features). They do
  not change the pool, so ranking claims stay comparable. The one free-input arm (§5) is reported
  separately.
- Trusted oracle: `proofhunter.oracle.rounding.rounding_half_up_once`
  (`(sum(prices) * (100 - discount) + 50) // 100`), computed before execution and outside the
  target.

### 3.3 Episode budget (identical for all arms)

| Rule | Value |
| --- | --- |
| Distinct paired base/head probes | at most 6 (`max_pairs`) |
| Fresh-head confirmation | 1 reserved, run only after a usable head mismatch (`max_confirmations`) |
| Retries | at most 2 per episode, only for `unusable` infra outcomes; a retry repeats the same candidate and counts toward invocations, not toward pairs |
| Invocation ceiling | 15 = 2·6 + 1 + 2 (`max_invocations_per_episode`) |
| Per-invocation timeout | 5 s (production `BudgetLimits`) |
| Coverage | `no_defect_found` needs the required clean coverage tags `empty`, `zero_discount`, `full_discount`, `half_cent`, `multi_item` (production coverage guard, applied identically to every arm) |
| Snap routing | **disabled** for every controlled comparison (`snap_routing = "disabled"` is a schema literal) |
| Seeds | `master_seed = 20261107`; replicate seeds `[1633385816, 3469185555, 2611436028, 2392925855, 277584464]` = first five `random.Random(20261107).randrange(2**32)` |

### 3.4 Pair outcome, confirmation, verdict

- Pair outcome (`Outcome`): `match` (base = head = expected), `mismatch` (base = expected,
  head ≠ expected), `base_mismatch` (base ≠ expected), `unusable` (timeout, exception, invalid
  output, trust or infrastructure failure).
- Confirmation (`Confirmation`): `reproduced` when a fresh head run returns the same mismatching
  value; else `not_reproduced` or `unusable`; `not_run` otherwise.
- Verdicts use the production rules (PRD §5, `src/proofhunter/oracle/verdict.py`):
  `confirmed_regression`, `confirmed_existing_defect`, `no_defect_found` (at least 3 distinct usable
  matches, all coverage tags, no mismatch or unresolved attempt), `inconclusive` otherwise.
- **Success** on a defect case = a `confirmed_*` verdict whose witness kind matches the label, within
  budget. On a clean control, any `confirmed_*` verdict is a false positive. `inconclusive` is never
  success.

## 4. Track B: SWE-smith repository-level test selection (design only)

- Scope: manifests, licenses, candidate representation, schema and label definitions.
  `SweSmithTask` rows record `dataset_id`, pinned `dataset_revision` and file hash, `repo`,
  `repo_commit`, SPDX license and redistribution status, `defect_origin`, grouped split
  (`split_group` = repository), and candidate tests.
- Candidate = an allowlisted test identifier (`TestCandidate.test_id`, strict pattern, at most 300
  chars) from the task's FAIL_TO_PASS / PASS_TO_PASS lists. A later bounded invocation is a fixed
  runner command with that id. Free-form commands are never allowed.
- Labels (future, after execution is qualified): positive = the test fails on the buggy revision
  and passes on the fixed one, reproduced; zero = usable run without exposure; unknown = anything
  else. Until then `label_status = "unverified_design_only"`.
- **Execution blocker.** `executed: Literal[false]` and
  `execution_status: Literal["blocked_no_qualified_sandbox"]` are schema literals. No SWE-smith
  repository, test or dataset code may execute anywhere: not on the coordinator host, not in a
  training GPU container, not in an unqualified sandbox. Freestyle failed strict metadata-denial
  qualification. The available Daytona tier is insufficient for the required isolation.
- **Unblock conditions** (all needed, then a new protocol version): (a) a sandbox provider passes
  the repository's isolation qualification: metadata-endpoint denial, egress allowlist, no
  credential exposure, fresh instance per run, enforced CPU/memory/time limits, verified teardown;
  (b) the qualification evidence is recorded and reviewed; (c) setup issues `research-protocol-v2`,
  replacing the literals. Track B results must never be presented as production EIG results unless
  reviewed Bayesian hypotheses, predictors and likelihoods genuinely apply.

## 5. Arms (Track A)

Every executing arm gets the same pre-execution context (`EpisodeContext`): base and head source,
unified diff, shared pool, built-in plan, case id and seed. It gets the same per-pair feedback
(`PairFeedback`: outcome plus expected/base/head integers) and the same budget. No arm receives
labels, family/template ids, template params, fix patches or test-split information.

| `arm_id` | Generator (plan) | Selector | Receives | May claim |
| --- | --- | --- | --- | --- |
| `fixed_order` | built-in | anchors, then boundary, constraint, seeded_fuzz; ties by `input_sha256` | context + feedback | deterministic floor |
| `uniform_random` | built-in | uniform over remaining pool, seeded | context + feedback | random floor |
| `simple_heuristic` | built-in | predictor-disagreement count, then fractional-cent residue, then basket size | context + feedback | non-learned heuristic |
| `builtin_eig` | built-in | production `proofhunter.bayes.select/update`, read-only | context + feedback | production selector reference |
| `builtin_ranker` | built-in | LightGBM LambdaRank checkpoint | context + feedback | ranker vs EIG with the plan held fixed |
| `qwen_untuned_eig` | Qwen2.5-Coder-7B-Instruct, untuned | EIG | context + feedback | factorial cell |
| `qwen_untuned_ranker` | untuned Qwen | ranker | context + feedback | factorial cell |
| `qwen_tuned_eig` | QLoRA/LoRA-tuned Qwen | EIG | context + feedback | factorial cell |
| `qwen_tuned_ranker` | tuned Qwen | ranker | context + feedback | factorial cell; with the three above it estimates ranker and generator effects and their interaction |
| `meta_muse_spark_eig` | production Meta port (`muse-spark` family; exact id pinned by the baselines lane, production default `muse-spark-1.3`) | EIG | context + feedback | current-system baseline, Snap disabled |
| `frontier_reviewer` | n/a | frontier model orders up to 6 pool candidates once; the harness executes that order | context only, **no execution feedback** | reviewer-style baseline |
| `frontier_agent` | n/a | frontier tool-using agent; one tool, `run_pair(input_sha256)`, restricted to the pool | context + feedback via the tool | strongest baseline for ranking claims |
| `frontier_agent_free_input` (optional) | n/a | same agent; may propose any legal input | context + feedback | reported separately; never pooled with ranking claims |

- Frontier model ids are **not** fixed in v1. The baselines lane verifies exact ids against the
  provider's model listing, freezes them in `research/baselines/models.json` with the verification
  date, and every row records `frontier_model_id`. Candidate OpenAI ids reported by the user
  (`gpt-6-astra`, `gpt-6-sol`, `gpt-6.1-sol`) are unverified until then.
- Generator: base model revision, tokenizer revision, prompt format (`gen-prompt-v1`) and adapter
  sha256 are frozen by the generator lane. Training uses train only, selection uses validation only.
  Provider is a lane decision: Fireworks managed LoRA SFT (`accounts/peli`,
  `qwen2p5-coder-7b-instruct`) if the adapter can be exported and hashed, otherwise QLoRA on Runpod.
- Generator outputs go through production `HypothesisPlanPayload` validation. Invalid output becomes
  the built-in plan, and the fallback is recorded (counted in the inconclusive analysis, not hidden).

## 6. Labels for learning

- Ranking rows (`RankingObservation`), one per executed decision:
  `positive` = usable head mismatch reproduced on fresh head (exposes the validated defect);
  `zero` = usable `match`; `unknown` = `unusable`, `base_mismatch`, non-reproduced or unusable
  confirmation, or missing evidence. Relevance is derived and checked by the validator.
- **Untested candidates are not negatives.** Rows exist only for executed candidates. Unknown rows
  are excluded from training targets.
- Exploration: collection policy is recorded. Exploratory rows carry `selection_probability` for
  off-policy correction. Collection covers train and validation only (test rows are rejected).
- Features are pre-execution only. Names starting with `label`, `relevance`, `outcome`,
  `observed`, `witness`, `expected`, `head_value`, `base_value`, `confirm`, `defect`, `family`,
  `template`, `split`, `verdict` are rejected.
- Generator examples (`GeneratorExample`): input = contract, behavior spec hash, allowed interface,
  base/head context files, diff. Target = hypotheses grounded in a reproduced witness
  (`grounding_witness_sha256`). Never includes template params or fix patches. Train/validation
  only. Teacher (distillation) provenance is recorded when used.

## 7. Metrics

Reported per arm, per measurement mode, per split (validation for development, sealed test for
the final claim):

- Recall@1/3/6: share of defect cases with a confirmed matching witness within k pairs.
- Precision of `confirmed_*` verdicts; false-positive rate on clean controls.
- Inconclusive rate, with reason codes; generator-fallback rate.
- Reproduction/confirmation rate (confirmed / head mismatches).
- Per-family recall (and per-repository for Track B in future versions).
- Coverage and exclusion: cases filtered and why; probes lost to unusable outcomes and retries.
- Pairs-to-first-confirmed-witness. Misses are **right-censored** at the budget, never assigned a
  value (`censored = true`, `pairs_to_first_confirmed_witness = null`).
- Latency components (`Timings`): queue, generation, features, ranking, sandbox startup, execution,
  confirmation, cleanup, wall; cold start flagged; p50/p95 per arm; time-to-first-witness.
- Cost components (`TokenUsage`, `cost_usd`, `cost_status` = measured/estimated/unknown) plus the
  ledger, including failed and retried calls. Unknown costs stay unknown and are not counted as zero.

## 8. Statistics

- Unit = case. Intervals = percentile 95% case bootstrap, 10000 resamples, seed `20261107`,
  grouped by `family_id` for the headline claims (case-level also reported).
- Paired comparisons on identical (case, seed) sets: per-case difference, bootstrap interval of the
  mean difference. Factorial: main effects and interaction from the four Qwen cells.
- Replicates: the five preregistered seeds. Stochastic model arms report replicate spread.
- No test-split peeking: model and hyperparameter selection use validation only. The test split
  runs once per frozen checkpoint set.
- **Offline, recorded and live measurements are reported separately and never pooled.**
  `measurement_mode` is a required field on every row.

## 9. What a result may claim

- Track A results apply only to `checkout-rounding@1` cases from the reviewed template families,
  under this budget and pool. They do not apply to arbitrary repositories.
- An improvement claim needs a paired 95% interval excluding zero on the sealed test split, in one
  stated measurement mode, against the named arm.
- Quality claims must be shown next to latency and cost. "Worth it" is a stated trade-off, not a
  verdict.
- Offline fixture results are labelled offline. Live claims need live rows.
- Track B v1 supports no empirical claim.

## 10. Spend policy

- Status: **user-approved, uncapped total** ("don't worry about spending cap. just go fast").
  The earlier proposed 50 USD total (pilot 5, training 20, evaluation 15, contingency 10) is recorded
  as superseded.
- Mandatory guards:
  1. Every paid call or provisioned resource gets a `LedgerEntry` (`paid = true`) with a pre-launch
     job estimate, pricing reference and date.
  2. Idle resources shut down automatically (Fireworks 15 min, Runpod 10 min). Teardown is verified
     by a provider listing recorded in the ledger (`teardown_verified`).
  3. Coordinator check-in before any single job estimated above **50 USD**. The ledger rejects
     such entries without `coordinator_checkin_ref`.
  4. Failed and retried calls count as cost. Estimates are never presented as measured.
- Providers and keys (environment variable names only; values never written anywhere):
  `PROOFHUNTER_OPENAI_API_KEY` (frontier arms), `PROOFHUNTER_FIREWORKS_API_KEY` (generator
  training/inference), `RUNPOD_API_KEY` (fallback GPU), `HF_TOKEN` (read-only model/dataset
  download), `MODEL_API_KEY` (Meta baseline), `SUBQ_API_KEY` (unused in controlled comparisons).
  Per-provider hard stops are in `spend-policy.json`.

## 11. Security

- Only reviewed first-party code executes: production harness fixtures and reviewed research
  templates. No arbitrary dataset or target code runs on the coordinator host, in a training
  container, or in an unqualified sandbox.
- Model outputs are untrusted data. They are parsed into production contracts or rejected and never
  executed.
- No secrets in files, rows, logs, commits or prompts.

## 12. Phases

R0 setup (this commit) → R1 dataset + reproduction pilot → R2 ranking observations / generator
examples → R3 ranker + generator training (validation selection, frozen checkpoints) → R4 integrated
evaluation of all arms → R5 measurement and report → R6 independent verification and teardown audit.
Lane ownership and interfaces: [`../OWNERSHIP.md`](../OWNERSHIP.md).
