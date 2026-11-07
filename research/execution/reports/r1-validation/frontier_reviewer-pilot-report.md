# R1 feasibility pilot

Mode: live (reviewed templates, one isolated local child process per invocation; not a cloud or sandbox timing). Split: validation. Cases: 50. Seeds: 5. Implementation: `4e9d74d5ded2a4b367a46815ea1b1234b26cbdae`.

## Dataset checks

Defect cases 46, witnesses reproduced 46, not reproduced 0, defect cases without an exposing pool input 11, EIG-modeled defect cases 17, source review failures none.

## Arms (descriptive, small sample)

| arm | episodes | usable | recall@1 | recall@3 | recall@6 | clean FP | mean invocations | p50 ms | p95 ms | startup ms | exec ms | cleanup ms | USD per usable case |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| frontier_reviewer | 250 | 250 | 0.674 | 0.674 | 0.674 | 0 | 6.42 | 2.09e+03 | 3.92e+03 | 17.8 | 99.6 | 1.31 | 0.0027 |

## Projected full run

639 cases x 13 arms x 5 repetitions x 6.42 executions per episode = 2.67e+05 executions; worst case 623025 (15 per episode). Projected dollars for measured arms 8.63; unknown for builtin_eig, builtin_ranker, fixed_order, frontier_agent, frontier_agent_free_input, meta_muse_spark_eig, qwen_tuned_eig, qwen_tuned_ranker, qwen_untuned_eig, qwen_untuned_ranker, simple_heuristic, uniform_random. Execution is local reviewed template code in isolated child processes (0 USD). Model-arm dollars stay unknown until their lanes report measured per-episode costs.
