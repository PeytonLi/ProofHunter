# R1 feasibility pilot

Mode: offline (reviewed templates, one isolated local child process per invocation; not a cloud or sandbox timing). Split: validation. Cases: 50. Seeds: 5. Implementation: `4e9d74d5ded2a4b367a46815ea1b1234b26cbdae`.

## Dataset checks

Defect cases 46, witnesses reproduced 46, not reproduced 0, defect cases without an exposing pool input 11, EIG-modeled defect cases 17, source review failures none.

## Arms (descriptive, small sample)

| arm | episodes | usable | recall@1 | recall@3 | recall@6 | clean FP | mean invocations | p50 ms | p95 ms | startup ms | exec ms | cleanup ms | USD per usable case |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| builtin_eig | 250 | 250 | 0.239 | 0.348 | 0.435 | 0 | 9.36 | 244 | 354 | 21.4 | 141 | 1.7 | 0 |
| fixed_order | 250 | 250 | 0.239 | 0.239 | 0.435 | 0 | 9.48 | 270 | 386 | 24.4 | 165 | 1.9 | 0 |
| simple_heuristic | 250 | 250 | 0.522 | 0.543 | 0.565 | 0 | 7.52 | 201 | 335 | 16.9 | 111 | 1.39 | 0 |
| uniform_random | 250 | 250 | 0.2 | 0.387 | 0.474 | 0 | 9.12 | 279 | 468 | 25.8 | 173 | 1.9 | 0 |

## Projected full run

639 cases x 13 arms x 5 repetitions x 8.87 executions per episode = 3.68e+05 executions; worst case 623025 (15 per episode). Projected dollars for measured arms 0; unknown for builtin_ranker, frontier_agent, frontier_agent_free_input, frontier_reviewer, meta_muse_spark_eig, qwen_tuned_eig, qwen_tuned_ranker, qwen_untuned_eig, qwen_untuned_ranker. Execution is local reviewed template code in isolated child processes (0 USD). Model-arm dollars stay unknown until their lanes report measured per-episode costs.
