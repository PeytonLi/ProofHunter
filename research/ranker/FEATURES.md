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
