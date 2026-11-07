# Offline benchmark (descriptive only)

Latency is local **offline_fixture**, not cloud latency. Provider mode: recorded/offline_fixture; no live calls or measured cloud cost.

| arm | recall@1..6 | clean FP/runs | median detected pairs | censored defects | inconclusive/runs | base/head/confirm | p50/p95 ms (offline_fixture) |
|---|---|---|---|---|---|---|---|
| fixed_boundary | 0.000, 0.000, 0.000, 0.750, 0.750, 0.750 | 0/10 | 4.0 | 5/20 | 0/30 | 150/150/15 | 2.847/3.175 |
| random | 0.250, 0.350, 0.550, 0.600, 0.600, 0.650 | 0/10 | 2.0 | 7/20 | 14/30 | 133/133/13 | 2.953/3.170 |
| eig_deterministic | 0.750, 0.750, 1.000, 1.000, 1.000, 1.000 | 0/10 | 1.0 | 0/20 | 0/30 | 90/90/20 | 2.875/6.513 |
| meta_snap_eig | 0.750, 0.750, 0.750, 0.750, 0.750, 0.750 | 0/10 | 1.0 | 5/20 | 0/30 | 105/105/15 | 2.778/4.695 |

fixed_boundary: inconclusive reasons {}; recorded route fallbacks 0/0; stub decisions 0.


random: inconclusive reasons {'coverage_insufficient': 14}; recorded route fallbacks 0/0; stub decisions 0.


eig_deterministic: inconclusive reasons {}; recorded route fallbacks 0/0; stub decisions 0.


meta_snap_eig: inconclusive reasons {}; recorded route fallbacks 0/105; stub decisions 45.


## Paired case-bootstrap differences

Case means include all preregistered seeds. 2000 resamples, seed 2000; percentile 95% intervals; defective cases only.

| contrast | metric | mean difference | 95% CI |
|---|---|---|---|
| eig_deterministic - random | recall@3 | 0.450 | [0.200, 0.800] |
| eig_deterministic - random | recall@6 | 0.350 | [0.050, 0.800] |
| eig_deterministic - fixed_boundary | recall@3 | 1.000 | [1.000, 1.000] |
| eig_deterministic - fixed_boundary | recall@6 | 0.250 | [0.000, 0.750] |
| meta_snap_eig - eig_deterministic | recall@3 | -0.250 | [-0.750, 0.000] |
| meta_snap_eig - eig_deterministic | recall@6 | -0.250 | [-0.750, 0.000] |

Misses are right-censored at six pairs, not assigned a seventh pair. Medians condition on detection; no superiority inference. The fixed bank knows the two rounding predictors and may miss item-count and other unmodeled families. No bank tuning was performed.

## Separate adversarial infrastructure suite (not detection metrics)

| scenario | verdict reason | observation failures |
|---|---|---|
| timeout_head | coverage_insufficient | {'timeout': 6} |
| invalid_output | coverage_insufficient | {'invalid': 6} |
| flaky_confirmation | nondeterministic | {} |
| isolation_unverified | isolation_unverified | {} |
| unknown_source | isolation_unverified | {'denied': 12} |
