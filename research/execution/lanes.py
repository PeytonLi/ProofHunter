"""Resolves other lanes' frozen factories lazily; a missing lane yields a recorded skip."""

from __future__ import annotations

import importlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from research.schemas.interfaces import CaseStore, HypothesisGenerator, Ledger, Selector
from research.schemas.models import ArmSpec, LedgerEntry, LedgerPhase, MeasurementMode

SINGLE_JOB_CHECKIN_USD = 50.0

FACTORY_SELECTORS = {"fixed_order", "uniform_random", "simple_heuristic"}


class LaneMissing(Exception):
    """A lane module or a frozen artifact needed by an arm is not available."""


def _attr(module: str, name: str) -> Any:
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as error:
        raise LaneMissing(f"{module} not integrated ({error.name})") from error
    value = getattr(loaded, name, None)
    if not callable(value):
        raise LaneMissing(f"{module}.{name} not implemented")
    return value


@dataclass(frozen=True)
class FrozenArtifacts:
    """Frozen evaluation inputs (``--config``). Hashes are checked before any episode runs."""

    ranker_checkpoint: Path | None = None
    ranker_checkpoint_sha256: str | None = None
    generator_adapter: Path | None = None
    generator_adapter_sha256: str | None = None
    generator_revision: str | None = None
    frontier_model_id: str | None = None
    arms: Sequence[str] | None = None

    @classmethod
    def load(cls, path: Path | None) -> FrozenArtifacts:
        if path is None:
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        for key in ("ranker_checkpoint", "generator_adapter"):
            if data.get(key) is not None:
                data[key] = (path.parent / data[key]).resolve()
        return cls(**data)


def _check_hash(path: Path | None, digest: str | None, what: str) -> Path:
    import hashlib

    if path is None or digest is None:
        raise LaneMissing(f"{what} path and sha256 must be frozen in the config")
    if not path.is_file():
        raise LaneMissing(f"{what} not found: {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise LaneMissing(f"{what} sha256 differs from the frozen config")
    return path


@dataclass
class ArmFactory:
    artifacts: FrozenArtifacts = field(default_factory=FrozenArtifacts)
    ledger: Ledger | None = None
    mode: MeasurementMode = MeasurementMode.OFFLINE
    phase: LedgerPhase = LedgerPhase.PILOT
    estimated_job_usd: dict[str, float] = field(default_factory=dict)

    def _provider_kwargs(self, arm_id: str) -> dict[str, Any]:
        """Ledger/mode/phase for paid provider arms; refuses without a pre-launch estimate."""
        if self.mode is not MeasurementMode.LIVE:
            raise LaneMissing(f"{arm_id} calls a paid provider; run it with --mode live")
        if self.ledger is None:
            raise LaneMissing(f"{arm_id} needs a ledger")
        estimate = self.estimated_job_usd.get(arm_id)
        if estimate is None:
            raise LaneMissing(f"{arm_id} needs a pre-launch estimate (--estimate {arm_id}=USD)")
        if estimate > SINGLE_JOB_CHECKIN_USD:
            raise LaneMissing(f"{arm_id} estimate {estimate} USD needs a coordinator check-in")
        return {"ledger": self.ledger, "mode": self.mode, "phase": self.phase,
                "estimated_job_usd": estimate}

    def selector(self, spec: ArmSpec, seed: int) -> Selector:
        selector: Selector
        if spec.selector in FACTORY_SELECTORS or spec.selector == "eig":
            arm_id = spec.arm_id if spec.selector in FACTORY_SELECTORS else "builtin_eig"
            selector = _attr("research.baselines.selectors", "make_selector")(arm_id, seed=seed)
            return selector
        if spec.selector == "lambdarank":
            checkpoint = _check_hash(self.artifacts.ranker_checkpoint,
                                     self.artifacts.ranker_checkpoint_sha256, "ranker checkpoint")
            selector = _attr("research.ranker.selector", "load_ranker_selector")(
                checkpoint, arm_id=spec.arm_id)
            return selector
        model_id = self.artifacts.frontier_model_id
        if spec.selector in ("frontier_reviewer", "frontier_agent") and model_id is None:
            if self.phase is not LedgerPhase.PILOT:
                raise LaneMissing("frontier_model_id must be frozen in the config")
            models = _attr("research.baselines.config", "load_models")()
            model_id = models["frontier"]["primary_model_id"]
        if spec.selector == "frontier_reviewer":
            selector = _attr("research.baselines.frontier", "frontier_reviewer")(
                model_id, **self._provider_kwargs(spec.arm_id))
            return selector
        if spec.selector == "frontier_agent":
            selector = _attr("research.baselines.frontier", "frontier_agent")(
                model_id, free_input=spec.candidate_freedom == "free_input_documented",
                **self._provider_kwargs(spec.arm_id))
            return selector
        raise LaneMissing(f"no selector factory for {spec.selector!r}")

    def generator(self, spec: ArmSpec) -> HypothesisGenerator | None:
        generator: HypothesisGenerator
        if spec.generator in (None, "builtin_plan"):
            return None
        if spec.generator == "meta_muse_spark":
            kwargs = self._provider_kwargs(spec.arm_id)
            generator = _attr("research.baselines.meta", "meta_generator")(
                ledger=kwargs.pop("ledger"))
            for key, value in kwargs.items():
                setattr(generator, key, value)
            return generator
        if spec.generator in ("qwen_untuned", "qwen_qlora"):
            revision = self.artifacts.generator_revision
            if revision is None:
                raise LaneMissing("generator_revision must be frozen in the config")
            adapter = None
            if spec.generator == "qwen_qlora":
                adapter = _check_hash(self.artifacts.generator_adapter,
                                      self.artifacts.generator_adapter_sha256, "generator adapter")
            generator = _attr("research.generator.model", "load_generator")(
                spec.generator, adapter=adapter, revision=revision)
            return generator
        raise LaneMissing(f"no generator factory for {spec.generator!r}")


def open_store(root: Path) -> CaseStore:
    return cast(CaseStore, _attr("research.data.store", "open_store")(root))


class JsonlLedger:
    """Append-only JSONL ledger; used only when the measurement lane is not integrated."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def append(self, entry: LedgerEntry) -> None:
        checked = LedgerEntry.model_validate_json(entry.model_dump_json())
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(checked.model_dump_json() + "\n")

    def entries(self) -> Sequence[LedgerEntry]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return tuple(LedgerEntry.model_validate_json(line) for line in lines if line.strip())


def open_ledger(path: Path) -> Ledger:
    try:
        return cast(Ledger, _attr("research.metrics.ledger", "open_ledger")(path))
    except LaneMissing:
        return JsonlLedger(path)
