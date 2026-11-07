# R1 feasibility pilot

Mode: offline (reviewed templates, one isolated local child process per invocation; not a cloud or sandbox timing). Split: validation. Cases: 50. Seeds: 5. Implementation: `0c8a09a7028d1cc3a767c9598af00f1c0f9d9b84`.

## Dataset checks

Defect cases 46, witnesses reproduced 46, not reproduced 0, defect cases without an exposing pool input 11, EIG-modeled defect cases 17, source review failures none.

## Arms (descriptive, small sample)

| arm | episodes | usable | recall@1 | recall@3 | recall@6 | clean FP | mean invocations | p50 ms | p95 ms | startup ms | exec ms | cleanup ms | USD per usable case |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| learned_ranker | 250 | 250 | 0.13 | 0.283 | 0.478 | 0 | 10 | 284 | 428 | 31.4 | 159 | 1.78 | 0 |
| learned_ranker_no_belief | 250 | 250 | 0.152 | 0.304 | 0.304 | 0 | 9.84 | 277 | 351 | 30.2 | 151 | 1.76 | 0 |

## Projected full run

639 cases x 13 arms x 5 repetitions x 9.94 executions per episode = 4.13e+05 executions; worst case 623025 (15 per episode). Projected dollars for measured arms 0; unknown for builtin_eig, builtin_ranker, fixed_order, frontier_agent, frontier_agent_free_input, frontier_reviewer, meta_muse_spark_eig, qwen_tuned_eig, qwen_tuned_ranker, qwen_untuned_eig, qwen_untuned_ranker, simple_heuristic, uniform_random. Execution is local reviewed template code in isolated child processes (0 USD). Model-arm dollars stay unknown until their lanes report measured per-episode costs.
