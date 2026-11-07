"""Hypothesis generators: untuned Qwen control and the QLoRA adapter, identical I/O policy.

Generation runs on a GPU (Runpod) with ``research/training/pod/hf_generate.py``; the frozen
outputs are replayed here keyed by the rendered prompt hash so evaluation is reproducible
offline. Invalid, ungrounded or missing output falls back to the production built-in plan.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from proofhunter.contracts import HypothesisPlanPayload, builtin_hypothesis_payload
from research.generator import pins
from research.generator.decoding import POLICY
from research.generator.prompt import (
    PROMPT_FORMAT_VERSION,
    PlanParseError,
    check_grounded,
    parse_plan,
    prompt_sha256,
)
from research.schemas.interfaces import HypothesisGenerator
from research.schemas.models import GeneratorInput

GenerationKind = Literal["qwen_untuned", "qwen_qlora"]
GENERATIONS_FILE: Final = "generations.jsonl"
ADAPTER_MANIFEST: Final = "adapter_manifest.json"
DEFAULT_UNTUNED_DIR: Final = Path("research/artifacts/generator/untuned")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def adapter_sha256(adapter: Path) -> str:
    """Hash of the adapter weights + config (sorted name:sha lines)."""
    names = ("adapter_config.json", "adapter_model.safetensors")
    lines = [f"{n}:{sha256_file(adapter / n)}" for n in names]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def load_generations(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            out[str(row["prompt_sha256"])] = str(row["text"])
    return out


class QwenGenerator:
    """Replays frozen generations; same parse/ground/fallback rule for both kinds."""

    def __init__(self, generator_id: str, generations: Mapping[str, str]) -> None:
        self.generator_id = generator_id
        self._generations = dict(generations)
        self.stats: Counter[str] = Counter()

    def propose(self, prompt: GeneratorInput) -> HypothesisPlanPayload:
        if prompt.prompt_format_version != PROMPT_FORMAT_VERSION:
            raise ValueError("prompt format version mismatch")
        text = self._generations.get(prompt_sha256(prompt))
        if text is None:
            self.stats["missing"] += 1
            return builtin_hypothesis_payload()
        try:
            plan = parse_plan(text)
            check_grounded(plan, prompt)
        except PlanParseError:
            self.stats["invalid"] += 1
            return builtin_hypothesis_payload()
        self.stats["valid"] += 1
        return plan


def generator_id(kind: GenerationKind, adapter_hash: str | None) -> str:
    base = f"{kind}@{pins.BASE_REVISION[:12]}+{PROMPT_FORMAT_VERSION}+{POLICY.policy_id}"
    return f"{base}+{adapter_hash[:12]}" if adapter_hash else base


def load_generator(
    kind: GenerationKind, *, adapter: Path | None, revision: str
) -> HypothesisGenerator:
    if revision != pins.BASE_REVISION:
        raise ValueError(f"revision must be the pinned base {pins.BASE_REVISION}")
    if kind == "qwen_untuned":
        if adapter is not None:
            raise ValueError("the untuned control takes no adapter")
        return QwenGenerator(generator_id(kind, None),
                             load_generations(DEFAULT_UNTUNED_DIR / GENERATIONS_FILE))
    if adapter is None:
        raise ValueError("qwen_qlora needs an adapter directory")
    manifest = json.loads((adapter / ADAPTER_MANIFEST).read_text())
    digest = adapter_sha256(adapter)
    if manifest.get("adapter_sha256") != digest or manifest.get("base_revision") != revision:
        raise ValueError("adapter manifest does not match the adapter files or base revision")
    return QwenGenerator(generator_id(kind, digest), load_generations(adapter / GENERATIONS_FILE))
