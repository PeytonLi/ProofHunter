# Generator lane handoff (LANE=generator)

- Branch: `devin/1794090000-research/generator` (pushed, no PR). Code commit: `4625957` (this file is in the next commit on top of it).
- Setup SHA rebased onto: `cadc5b4d91828fec1adcb5341a11c2c466e57c1f` (schemas/interfaces from `13b9667`).
- Only owned paths changed: `research/generator/**`, `research/training/**`, `tests/research/generator/**`, `research/artifacts/generator/**`. No edits to src/, bench/, schemas/, fixtures/, tools/, config/, the production lock, research/cli.py or research/schemas.

## What is built
- `research/generator/prompt.py`: `PROMPT_FORMAT_VERSION="gen-prompt-v1"`, `build_input` (base+head checkout.py, unified diff, pinned behavior spec, no labels, no template params), `render_prompt` (exact Qwen ChatML text), `parse_plan` (strict production `HypothesisPlanPayload`, 16 KiB cap, first proposal per mode wins), `check_grounded` (refs must be inside the head file).
- `research/generator/examples.py`: `build_examples(store, split)`; refuses the test split; targets come from train/validation labels (family -> production mode, witness hash grounds modeled modes, refs = changed head lines; clean -> `other` only).
- `research/generator/model.py`: `load_generator("qwen_untuned"|"qwen_qlora", adapter=, revision=)`. Both kinds use the same prompt, decoding policy, parse/ground rule and built-in-plan fallback. Generations are produced on the GPU pod and replayed by rendered-prompt sha256; the adapter manifest hash and base revision are checked on load.
- `research/generator/decoding.py`: one policy for both arms (greedy, max 1024 new tokens, prompts over 4096 tokens excluded and reported, never truncated, bf16, seed 1234).
- `research/generator/scoring.py`: validation-only checkpoint score (mode recall, clean precision, ref hit rate, valid rate). Raises on non-validation examples.
- `research/generator/ledger.py`: JSONL ledger of frozen `LedgerEntry` records (validated on write and read).
- `research/training/qlora.py`: `QloraTrainer` (GeneratorTrainer). One Runpod pod: deadman, hash-locked deps, base model download with every weight sha256 and tokenizer git OID checked, untuned validation generations, QLoRA SFT (nf4, r=16, alpha=32, all attention+MLP projections, loss on completion only), one checkpoint per epoch, validation generations per checkpoint, local selection on validation, frozen adapter manifest.
- `research/training/runpod.py`: REST client, pod session that always deletes the pod, verifies it is gone from GET and list, has a local kill timer plus an on-pod deadman, refuses any job estimated above $50, and writes a ledger entry (with teardown_verified) even on failure.
- `research/generator/commands.py` `prepare_generator_examples`, `research/training/commands.py` `train_generator` (CLI dispatch already in research/cli.py; `--dry-run` prints the plan and max estimate).

## Pins and hashes
- Base and tokenizer: `Qwen/Qwen2.5-Coder-7B-Instruct@c03e6d358207e414f1eca0bb1891e29f1db0e242` (weights sha256 and tokenizer OIDs in `research/generator/pins.py`).
- Pod image: `runpod/pytorch@sha256:61a4aafb0094cd773f11eefa378929d5a687bd775febeb78eac62fc824141fb5` (torch 2.4.0, CUDA 12.4.1, Py 3.11).
- Pod lock `research/training/pod-requirements.lock` sha256: `b363098b8c256876b4757b44058d52909bc650490b35b6275d626e6e9070beb4`.
- Prompt format `gen-prompt-v1` sha256: `2980ad42b8bb739005da7878e6d6cf4d78413673907449f19e1d5a4d6e52abb9`.
- Behavior spec sha256 (same text as production `_CONTRACT`, test-checked): `9412f12f6839e1b8e07680de3551ffb6194857362f8095366fa9471250bbe7c2`.
- Decoding policy JSON sha256: `1005be16a4b5a55c2367a2ae9faffe708b68f8066305ee34456e4309975df36d`.
- Dataset hashes: none (no dataset examples exist yet). Adapter hash: none (no training run).

## Tests (all run on this commit)
- `research/.venv/bin/python -m pytest -q tests/research` -> 94 passed, 2 skipped. (`uv run --project research --locked` fails offline here: the uv cache lacks the scipy 1.18.1 wheel LightGBM needs; the existing research venv was used.)
- `research/.venv/bin/ruff check research/generator research/training tests/research/generator` -> all checks passed.
- `research/.venv/bin/mypy research/generator research/training tests/research/generator` -> no issues in 20 files.
- `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --locked pytest -q` -> 1271 passed, 2 skipped.
- `... uv run --locked ruff check .` -> all checks passed. `... mypy src bench tools tests/integration` -> no issues in 81 files.
- `... python tools/gate.py setup` -> PASSED. `... python tools/gate.py integrated` -> PASSED.
- `research/.venv/bin/python -m research.cli train-generator --train T --validation V --out O --run-id smoke-dry --smoke --dry-run` -> exit 0, max estimate $2.67 (RTX 4090, 3 h cap); wrong-split input exits 1.

## Costs
- $0 spent. No pod created, no paid calls, so the ledger is empty.

## Stopping point and remaining dependencies
1. `RUNPOD_API_KEY` is not in this session's environment, so nothing live ran: no untuned validation, no smoke training. Code is ready; with the key: `research train-generator ... --smoke` first.
2. Dataset branch `devin/1794090000-research/dataset` is not pushed yet. Needs a `CaseStore` factory (default `research.data.store:open_store`, overridable with `--store-factory`). Then: `research prepare-generator-examples --store ROOT --split train|validation --out F`, then `research train-generator`.
3. Family -> mode mapping uses family keywords (`per_line`/`per_item` -> per_item_rounding, `half_even`/`banker` -> half_even_rounding, else other). Check it against the dataset lane's final family ids.

## Known limitations
- Pod scripts (`research/training/pod/*.py`) have only been type- and lint-checked. They have never run on a GPU.
- The deadman uses `runpodctl remove pod` and falls back to killing PID 1. The local kill timer and the post-run delete + list check are the primary teardown.
- Costs recorded are `estimated` (costPerHr x wall time). Runpod billing is not read back.
- Greedy decoding instead of Qwen's sampling defaults, applied the same way to both arms.
