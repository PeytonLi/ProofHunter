# Ranker feature set `ranker-features-v1`

Pre-execution only: every value is computable from `EpisodeContext` (sources, diff, shared pool,
plan), `EpisodeState` (step, budget, guarded pool, missing coverage, prior pair feedback that every
arm sees) and the candidate. No feature reads labels, template params, family, split, the
candidate's own base/head output or confirmation. Names avoid `FORBIDDEN_FEATURE_PREFIXES`
(asserted at import and by tests). All values are floats.

| group | features | source |
|---|---|---|
| candidate | `strategy_<s>` one-hot, `is_anchor`, `tag_<coverage tag>`, `n_coverage_tags` | production `Candidate` |
| shape | `n_items`, `total_cents`, `log1p_total_cents`, `max_price`, `min_price`, `distinct_prices`, `all_equal`, `discount_percent`, `kept_percent`, `total_residue` (= total·kept mod 100), `fractional_cent`, `half_cent`, `line_residue_sum`, `lines_with_fraction`, `lines_at_or_above_half` | candidate input |
| predictor | `per_item_differs`, `half_even_differs`, `n_predictors_differ`, `abs_per_item_delta`, `abs_half_even_delta` (production `bayes.predict_*` vs H0) | candidate input |
| diff | `diff_added_lines`, `diff_removed_lines`, `diff_{added,removed}_{loop,round_call,floor_div,modulo,decimal,conditional,comparison,int_literal}` (regex counts) | `context.unified_diff` |
| context | `step`, `remaining_pairs`, `budget_fraction_used`, `pool_available`, `history_{matches,mismatches,base_mismatches,unusable}`, `missing_coverage_count`, `covers_missing_count` | `EpisodeState` |
| belief | `p_<hypothesis>` (H0, H1–H3, OTHER), `belief_entropy`, `eig_bits`, `p_mismatch` | production prior from `context.plan`, updated with `bayes.posterior` on usable match/mismatch history only (rounded to 12 dp like `bayes.update`) |

Ablations (`features.ABLATIONS`): `full`; `no_belief` (drops the belief group); `no_belief_no_predictor`
(also drops predictor disagreement, which is a belief-free proxy of the same signal).

## Observations and groups

`collect.collect_observations` runs reviewed sources only (dataset `templates.render` re-render
check, or the fixture reviewer) in a fresh `python -I -S` child with an empty environment and
rlimits. At each decision step the whole coverage-guarded pool is executed; each row records the
behaviour policy's choice probability (`eig_eps<e>`: ε-greedy over production EIG; `uniform_random`).
The chosen candidate's feedback advances the episode, which stops at the first reproduced
mismatch or after 6 steps. A LambdaRank group is one decision point `(case, seed, policy, step)`;
`unknown` rows are dropped (never zero), groups with < 2 labelled rows are dropped, metrics use
groups that contain a positive. `wall_ms` is the batch wall time amortised per invocation.

## Real-data designs (dataset `ba3d927d…1520`)

- **dense** (primary): `collect-observations --store dataset` with policy `eig_eps0.3` and replicate
  seed 1633385816 executes the whole guarded pool at each step of every train/validation case
  (reviewed template renderings only). Groups are decision points, as above. Train 439,377 rows
  (10,415 groups); validation 68,482 rows (1,642 groups, 747 with a positive).
- **logged**: the dataset lane's `eps0.3-heuristic-v1` log has one executed row per decision step,
  so decision-point groups are singletons. `relabel.refeaturize` replays each logged episode
  (pool `bayes.generate(builtin plan, pool_seed(candidate_seed, seed))`, pool hash checked, no
  guard, recorded feedback) and recomputes `ranker-features-v1` without executing anything.
  `train-ranker --group-by episode` groups one episode's picks; `--weighting ips` weights each
  row by `min(1/selection_probability, 50)` normalised to mean 1. Caveat: episodes stop at the
  first positive, so a positive is always the last pick and `step`/history features leak the
  stopping rule inside an episode group (top gain feature is `step`). Use dense as primary.
- Tuning and every reported comparison use dense **validation** decision points only
  (`python -m research.ranker.compare`). Test rows and test labels are never read.

## `ranker-features-v2` (real-v2)

v2 = v1 plus a `heuristic` group, all pre-execution and computed from the candidate and
`state.available` only:

| feature | definition |
|---|---|
| `n_distinct_predictions` | distinct values among H0, per-item and half-even predictions (1–3) |
| `residue_dist_half` | `|(total * (100-d)) % 100 - 50|` |
| `heuristic_score` | `10000*n_distinct + (50-residue_dist_half)*100 + min(n_items, 99)`: the spec `simple_heuristic` lexicographic key as one number |
| `heuristic_rank`, `heuristic_rank_frac`, `heuristic_is_top` | the candidate's position in the available set under that key (0 = the heuristic's pick) |

v1 lacked `n_distinct_predictions` (it had `n_predictors_differ`, which counts predictions
differing from H0), the folded residue distance, the combined key and any pool-relative rank.
`relabel.augment_v1` upgrades dense v1 rows to v2 exactly (dense groups hold the whole available
set; test `test_v1_augment_matches_v2_extractor`). The selector still loads v1 checkpoints
(model input = v2 minus the heuristic group). New ablations: `no_heuristic`, `no_diff`.

real-v2 result: 288 validation-only configs; v2_full validation MRR 0.352 vs heuristic 0.557
(see `research/artifacts/ranker/HANDOFF.md`).
