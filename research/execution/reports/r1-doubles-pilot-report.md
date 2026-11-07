# R1 feasibility pilot

Mode: offline (in-process reviewed template execution; not a cloud or sandbox timing). Split: validation. Cases: 12. Seeds: 5. Implementation: `163b5af2dcd5c2f702c4cda93e1cf45407b33778`.

## Dataset checks

Defect cases 9, witnesses reproduced 9, not reproduced 0, defect cases without an exposing pool input 0, EIG-modeled defect cases 6, source review failures none.

## Arms (descriptive, small sample)

| arm | episodes | usable | recall@1 | recall@3 | recall@6 | clean FP | mean invocations | p50 ms | p95 ms | USD per usable case |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| builtin_eig | 60 | 60 | 0.667 | 0.667 | 0.667 | 0 | 7.5 | 26.1 | 29.4 | 0 |
| fixed_order | 60 | 60 | 0 | 0.333 | 0.889 | 0 | 9 | 22 | 23.7 | 0 |
| uniform_random | 60 | 60 | 0.289 | 0.689 | 0.844 | 0 | 7.87 | 22.4 | 26.4 | 0 |

Skipped arms: simple_heuristic (no double for selector 'simple_heuristic')

## Projected full run

1010 cases x 13 arms x 5 repetitions x 8.12 executions per episode = 5.33e+05 executions; worst case 984750 (15 per episode). Projected dollars for measured arms 0; unknown for builtin_ranker, frontier_agent, frontier_agent_free_input, frontier_reviewer, meta_muse_spark_eig, qwen_tuned_eig, qwen_tuned_ranker, qwen_untuned_eig, qwen_untuned_ranker, simple_heuristic. Execution is local reviewed in-process code (0 USD). Model-arm dollars stay unknown until their lanes report measured per-episode costs.
