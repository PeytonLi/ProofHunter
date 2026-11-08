# Generator lane handoff (LANE=generator)

- Branch `devin/1794090000-research/generator`, on setup `cadc5b4d91828fec1adcb5341a11c2c466e57c1f`. Dataset input: branch `b48a61f9a9b53ba1a8bbd9cb89a53f86b7a30d1c`, tarball sha256 `046c59ca78329a9f31ea98d196bf95cd24571cc69f38b030851e5b47b151da98` (train.jsonl `cc5c1412...e5e48e9`, validation.jsonl `57a119d8...10c48866`, both match the dataset report).
- Only owned paths changed (`research/generator/**`, `research/training/**`, `tests/research/generator/**`, `research/artifacts/generator/**`). No shared schema/interface change.

## Results (validation only; test labels never read)
| arm | n | valid-plan parse rate | score | clean precision | mode recall | ref hit |
|---|---|---|---|---|---|---|
| untuned Qwen2.5-Coder-7B-Instruct @ c03e6d35 | 300 | 0.46 | 0.069 | 0.00 | n/a (no modeled-mode targets) | n/a |
| QLoRA tuned | not run | - | - | - | - | - |

- **No tuned adapter exists.** Coordinator deadline: the pod was deleted at 00:00:30 UTC after 51 min inside epoch 1 of 3. Checkpoints are written only at epoch end, so none was saved. `adapter_sha256`: none.
- The untuned arm proposes all three modes in every valid plan, so precision is 0. Every validation example is labeled `other`, so the validation score only measures "valid plan without a modeled rounding mode". Train has 1,958 `other`, 42 `per_item_rounding` and 0 `half_even_rounding`. This was reported to the data lane.
- The smoke run (4 train and 2 validation examples built from reviewed `fixtures/demo/revisions` sources, never executed) went end to end on the pod. Smoke adapter sha256 `394642670e665649c4f63a9652424975d12355c2468ea88ccdb9fb71726d7473`. It is a pipeline check only, not a result (`smoke-adapter-manifest.json`).

## Cost and teardown
- Pod `mozp9af8wlaf9h` (RTX 4090 secure, $0.89/h): 1.1425 h, **$1.017 estimated**. Create probe `nw0jfse5wbkg90`: $0.0007. Total about **$1.02**, max job estimate $7.12. Costs are estimates (costPerHr × wall time); Runpod billing was not read back.
- Teardown verified: DELETE, then GET `/pods/mozp9af8wlaf9h` returns nothing and the pod list is empty (`full-qlora-v1/teardown-evidence.json`). The ledger (`full-qlora-v1/ledger.jsonl`, frozen `LedgerEntry`) has 2 paid entries with `teardown_verified=true` and 2 unpaid usage-attribution entries.

## Pins
- Base and tokenizer `Qwen/Qwen2.5-Coder-7B-Instruct@c03e6d358207e414f1eca0bb1891e29f1db0e242`. Weight sha256s and tokenizer OIDs were verified on the pod.
- Image `runpod/pytorch@sha256:61a4aafb0094cd773f11eefa378929d5a687bd775febeb78eac62fc824141fb5`. Lock sha256 `b363098b8c256876b4757b44058d52909bc650490b35b6275d626e6e9070beb4`.
- Prompt `gen-prompt-v1` format sha256 `2980ad42...abb9`. Decoding policy sha256 `1005be16...df36d` (greedy, 1024 new tokens, 4096 prompt-token limit, no truncation). Train config `qlora-v1` (nf4, r16/α32, lr 2e-4, grad_accum 8, 3 epochs).
- Spec texts: the production spec `9412f12f...` and the dataset-lane spec `a9576375...` are both registered in `prompt.SPEC_TEXTS`, so dataset examples keep their hashes. The two lanes use different spec wording; this should be unified.

## Tests
- `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research` -> 94 passed, 2 skipped.
- Lane ruff and mypy (22 files) clean. Production `pytest -q` -> 1271 passed, 2 skipped. `ruff check .` and `mypy src bench tools tests/integration` are clean. `gate.py setup` and `gate.py integrated` PASSED.

## How to resume (adds about 3.5 h of GPU, about $3)
`podctl up` -> `podctl train --run-id ... --train T --val V --out O` (or `podctl finish --epoch 1` to stop after epoch 1 and freeze) -> `podctl down`, all with `RUNPOD_API_KEY` bound per command. One epoch takes more than 51 min on a 4090. Saving a mid-epoch checkpoint, or setting `max_steps`, would make a deadline-bounded run possible.

## Known limitations
- Runpod rejected `allowedCudaVersions` ("no instances available"), so it is dropped from the pod request.
- The on-pod idle and deadman guards fall back to `kill 1`, because `RUNPOD_API_KEY` is not injected into the pod. Local delete plus verify is the primary teardown.
