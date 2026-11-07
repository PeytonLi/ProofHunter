"""Checkpoint hash and deterministic-load checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from research.verification.artifacts import sha256_file, tree_hashes
from research.verification.gates import GateResult


def adapter_digest(root: Path) -> str:
    """sha256 over the sorted (relative path, file sha256) list of a directory."""
    payload = json.dumps(sorted(tree_hashes(root).items()), separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def audit_checkpoint_hashes(expected: Mapping[str, str], root: Path,
                            raw_rows: Iterable[Mapping[str, Any]] = ()) -> GateResult:
    """``expected`` maps a path (relative to ``root``) to its frozen sha256.

    Directories match either ``adapter_digest`` or, when ``expected`` lists files, the
    per-file sha256. Every checkpoint hash referenced by raw rows must be in ``expected``.
    """
    gate = GateResult("checkpoint_hashes")
    actual: dict[str, str] = {}
    for rel, want in expected.items():
        path = root / rel
        if not path.exists():
            gate.error("checkpoint_missing", "file not found", rel)
            continue
        got = sha256_file(path) if path.is_file() else adapter_digest(path)
        actual[rel] = got
        if got != want:
            gate.error("checkpoint_hash_mismatch", f"{got} != frozen {want}", rel)
    known = set(expected.values())
    referenced: set[str] = set()
    for row in raw_rows:
        models = row.get("models") or {}
        for ref in ("ranker_checkpoint_sha256", "adapter_sha256"):
            if models.get(ref):
                referenced.add(str(models[ref]))
    for h in sorted(referenced - known):
        gate.error("unverified_checkpoint_reference", "raw rows cite a hash not supplied", h)
    gate.stats.update(checked=actual, referenced=sorted(referenced))
    return gate


def check_deterministic(load: Callable[[], Any], probe: Callable[[Any], Any], *,
                        name: str, runs: int = 2) -> GateResult:
    """Load ``runs`` times; ``probe(model)`` must return identical JSON each time."""
    gate = GateResult(f"deterministic_load:{name}")
    digests = []
    for _ in range(runs):
        out = probe(load())
        digests.append(hashlib.sha256(json.dumps(out, sort_keys=True,
                                                 default=repr).encode()).hexdigest())
    gate.stats["probe_digests"] = digests
    if len(set(digests)) != 1:
        gate.error("nondeterministic_load", f"{len(set(digests))} distinct probe outputs")
    return gate


def lightgbm_determinism(model_file: Path, *, rows: int = 256, seed: int = 20261107
                         ) -> GateResult:
    """Two independent Booster loads give bit-identical predictions on seeded inputs."""
    import lightgbm as lgb
    import numpy as np

    def load() -> Any:
        return lgb.Booster(model_file=str(model_file))

    def probe(booster: Any) -> Any:
        x = np.random.default_rng(seed).normal(size=(rows, booster.num_feature()))
        preds = booster.predict(x)
        return {"num_feature": booster.num_feature(), "num_trees": booster.num_trees(),
                "pred_sha256": hashlib.sha256(np.asarray(preds, dtype="<f8").tobytes()
                                              ).hexdigest()}

    gate = check_deterministic(load, probe, name=model_file.name)
    gate.stats["file_sha256"] = sha256_file(model_file)
    return gate
