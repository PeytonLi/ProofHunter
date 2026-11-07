# R1 feasibility pilot

Mode: live (reviewed templates, one isolated local child process per invocation; not a cloud or sandbox timing). Split: validation. Cases: 50. Seeds: 5. Implementation: `4e9d74d5ded2a4b367a46815ea1b1234b26cbdae`.

## Dataset checks

Defect cases 46, witnesses reproduced 46, not reproduced 0, defect cases without an exposing pool input 11, EIG-modeled defect cases 17, source review failures none.

## Arms (descriptive, small sample)

| arm | episodes | usable | recall@1 | recall@3 | recall@6 | clean FP | mean invocations | p50 ms | p95 ms | startup ms | exec ms | cleanup ms | USD per usable case |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| frontier_agent | 250 | 250 | 0.735 | 0.761 | 0.761 | 0 | 5.75 | 1.69e+03 | 9.34e+03 | 18.9 | 86.2 | 1.21 | 0.0101 |

## Projected full run

639 cases x 13 arms x 5 repetitions x 5.75 executions per episode = 2.39e+05 executions; worst case 623025 (15 per episode). Projected dollars for measured arms 32.3; unknown for builtin_eig, builtin_ranker, fixed_order, frontier_agent_free_input, frontier_reviewer, meta_muse_spark_eig, qwen_tuned_eig, qwen_tuned_ranker, qwen_untuned_eig, qwen_untuned_ranker, simple_heuristic, uniform_random. Execution is local reviewed template code in isolated child processes (0 USD). Model-arm dollars stay unknown until their lanes report measured per-episode costs.
