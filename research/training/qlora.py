"""QLoRA trainer for Qwen2.5-Coder-7B-Instruct on one Runpod GPU (Fireworks cannot train it).

Flow (single pod): deadman -> hash-locked deps -> verified pinned base -> untuned validation
generations -> QLoRA SFT with one checkpoint per epoch -> validation generations per checkpoint.
Selection is local and uses validation examples only; test examples are refused.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from research.generator import pins
from research.generator.completion import completion_text
from research.generator.decoding import POLICY
from research.generator.ledger import JsonlLedger
from research.generator.model import (
    ADAPTER_MANIFEST,
    GENERATIONS_FILE,
    adapter_sha256,
    load_generations,
    sha256_file,
)
from research.generator.prompt import (
    PROMPT_FORMAT_VERSION,
    format_sha256,
    prompt_sha256,
    render_prompt,
)
from research.generator.scoring import GeneratorScore, score_generations
from research.schemas.models import GeneratorExample, Split
from research.training.runpod import PodSession, pod_session

POD_DIR: Final = Path(__file__).parent / "pod"
LOCK: Final = Path(__file__).parent / "pod-requirements.lock"
REMOTE: Final = "/workspace/ph"


class TrainConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: str = "qlora-v1"
    seed: int = 20261107
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    target_modules: tuple[str, ...] = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj",
                                       "up_proj", "down_proj")
    learning_rate: float = 2e-4
    epochs: int = 3
    grad_accum: int = 8
    warmup_steps: int = 10
    max_seq_tokens: int = POLICY.max_prompt_tokens + POLICY.max_new_tokens
    max_steps: int | None = None
    quantization: str = "nf4-double-quant-bf16-compute"


SMOKE_CONFIG: Final = TrainConfig(config_id="qlora-smoke", epochs=1, grad_accum=1, max_steps=4,
                                  warmup_steps=1)


def sft_rows(examples: Iterable[GeneratorExample]) -> list[dict[str, str]]:
    rows = []
    for ex in examples:
        if ex.split not in (Split.TRAIN, Split.VALIDATION):
            raise PermissionError("test examples are never used by the generator lane")
        rows.append({"id": ex.example_id, "split": ex.split.value,
                     "prompt": render_prompt(ex.input), "completion": completion_text(ex)})
    return rows


def prompt_rows(examples: Iterable[GeneratorExample]) -> list[dict[str, str]]:
    return [{"id": ex.example_id, "prompt_sha256": prompt_sha256(ex.input),
             "prompt": render_prompt(ex.input)} for ex in examples]


def model_pins() -> dict[str, object]:
    return {"repo": pins.BASE_MODEL, "revision": pins.BASE_REVISION,
            "sha256": pins.SAFETENSORS_SHA256, "oids": pins.FILE_OIDS}


def _jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> str:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    return sha256_file(path)


@dataclass(frozen=True)
class Selection:
    checkpoint: str
    score: GeneratorScore
    val_loss: float | None


class QloraTrainer:
    """``GeneratorTrainer``: returns the selected adapter's sha256."""

    def __init__(self, *, run_id: str, config: TrainConfig | None = None, gpu: str = "rtx4090",
                 max_hours: float = 3.0, ledger: JsonlLedger | None = None) -> None:
        self.run_id = run_id
        self.config = config or TrainConfig()
        self.gpu = gpu
        self.max_hours = max_hours
        self.ledger = ledger or JsonlLedger()

    def stage(self, train: list[GeneratorExample], validation: list[GeneratorExample],
              stage_dir: Path) -> dict[str, str]:
        for name in ("hf_generate.py", "train_qlora.py", "fetch_model.py", "bootstrap.sh"):
            shutil.copy(POD_DIR / name, stage_dir / name)
        shutil.copy(LOCK, stage_dir / "pod-requirements.lock")
        (stage_dir / "model_pins.json").write_text(json.dumps(model_pins(), sort_keys=True))
        (stage_dir / "policy.json").write_text(POLICY.model_dump_json())
        cfg = self.config.model_dump(mode="json")
        cfg["target_modules"] = list(self.config.target_modules)
        (stage_dir / "config.json").write_text(json.dumps(cfg, sort_keys=True))
        return {
            "sft_sha256": _jsonl(stage_dir / "sft.jsonl", sft_rows([*train, *validation])),
            "val_prompts_sha256": _jsonl(stage_dir / "val_prompts.jsonl", prompt_rows(validation)),
        }

    def _remote(self, pod: PodSession, stage_dir: Path, out_dir: Path) -> None:
        pod.run(f"mkdir -p {REMOTE}")
        for item in sorted(stage_dir.iterdir()):
            pod.put(item, f"{REMOTE}/{item.name}")
        deadman = int(self.max_hours * 3600)
        pod.run(f"cd {REMOTE} && bash bootstrap.sh {deadman}", timeout=3600)
        gen = (f"cd {REMOTE} && python hf_generate.py --model-dir /workspace/model "
               f"--prompts val_prompts.jsonl --policy policy.json")
        pod.run(f"{gen} --out untuned.jsonl", timeout=7200)
        pod.get(f"{REMOTE}/untuned.jsonl", out_dir / "untuned" / GENERATIONS_FILE)
        pod.run(f"cd {REMOTE} && python train_qlora.py --model-dir /workspace/model "
                f"--data sft.jsonl --config config.json --out ckpt", timeout=int(deadman))
        pod.get(f"{REMOTE}/ckpt/train_log.json", out_dir / "train_log.json")
        log = json.loads((out_dir / "train_log.json").read_text())
        for epoch in log["epochs"]:
            name = epoch["checkpoint"]
            pod.run(f"{gen} --adapter ckpt/{name} --out ckpt/{name}/{GENERATIONS_FILE}",
                    timeout=7200)
            pod.get(f"{REMOTE}/ckpt/{name}", out_dir / "checkpoints" / name)

    def select(self, validation: list[GeneratorExample], out_dir: Path) -> Selection:
        log = json.loads((out_dir / "train_log.json").read_text())
        best: Selection | None = None
        for epoch in log["epochs"]:
            ckpt = out_dir / "checkpoints" / epoch["checkpoint"]
            gens = load_generations(ckpt / GENERATIONS_FILE)
            cand = Selection(epoch["checkpoint"], score_generations(validation, gens),
                             epoch["val_loss"])
            key = (cand.score.score, -(cand.val_loss or 0.0))
            if best is None or key > (best.score.score, -(best.val_loss or 0.0)):
                best = cand
        if best is None:
            raise RuntimeError("training produced no checkpoints")
        return best

    def freeze(self, sel: Selection, validation: list[GeneratorExample], out_dir: Path,
               hashes: dict[str, str]) -> str:
        adapter = out_dir / "adapter"
        adapter.mkdir(parents=True, exist_ok=True)
        src = out_dir / "checkpoints" / sel.checkpoint
        for name in ("adapter_config.json", "adapter_model.safetensors", GENERATIONS_FILE):
            shutil.copy(src / name, adapter / name)
        digest = adapter_sha256(adapter)
        untuned = score_generations(validation,
                                    load_generations(out_dir / "untuned" / GENERATIONS_FILE))
        manifest = {
            "schema": "research-generator-adapter-manifest-v1", "adapter_sha256": digest,
            "base_model": pins.BASE_MODEL, "base_revision": pins.BASE_REVISION,
            "tokenizer_revision": pins.TOKENIZER_REVISION, "pod_image": pins.POD_IMAGE,
            "pod_lock_sha256": sha256_file(LOCK), "prompt_format_version": PROMPT_FORMAT_VERSION,
            "prompt_format_sha256": format_sha256(), "decoding": POLICY.model_dump(mode="json"),
            "train_config": self.config.model_dump(mode="json"), "selected": sel.checkpoint,
            "selection_split": "validation", "validation_score": asdict(sel.score),
            "untuned_validation_score": asdict(untuned), "val_loss": sel.val_loss,
            "files": {p.name: sha256_file(p) for p in sorted(adapter.iterdir())},
            **hashes,
        }
        (adapter / ADAPTER_MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True))
        return digest

    def train(self, train: Iterable[GeneratorExample], validation: Iterable[GeneratorExample],
              out_dir: Path) -> str:
        train_l, val_l = list(train), list(validation)
        if any(e.split is not Split.TRAIN for e in train_l) or any(
                e.split is not Split.VALIDATION for e in val_l):
            raise PermissionError("train/validation splits only")
        if not train_l or not val_l:
            raise ValueError("need train and validation examples")
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            hashes = self.stage(train_l, val_l, stage)
            listing = "".join(f"{p.name}:{sha256_file(p)}\n" for p in sorted(stage.iterdir()))
            hashes["stage_files_sha256"] = hashlib.sha256(listing.encode()).hexdigest()
            with pod_session(run_id=self.run_id, gpu=self.gpu, max_hours=self.max_hours,
                             ledger=self.ledger) as pod:
                try:
                    self._remote(pod, stage, out_dir)
                finally:
                    (out_dir / "pod_log.txt").write_text("\n".join(pod.log))
        sel = self.select(val_l, out_dir)
        return self.freeze(sel, val_l, out_dir, hashes)
