"""Schema conformance: every row validated by both the frozen Pydantic model and JSON Schema."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
from pydantic import ValidationError

from research.schemas.export import SCHEMA_DIR, render
from research.schemas.models import SCHEMA_MODELS
from research.verification.gates import GateResult


def audit_frozen_schemas() -> GateResult:
    """Exported JSON Schemas still equal the models' render (no silent schema drift)."""
    gate = GateResult("schemas_frozen")
    for name in SCHEMA_MODELS:
        path = SCHEMA_DIR / f"{name}.schema.json"
        if not path.exists() or path.read_text() != render(name):
            gate.error("schema_drift", "JSON Schema differs from model", name)
    gate.stats["models"] = len(SCHEMA_MODELS)
    return gate


def audit_rows(rows: Iterable[Mapping[str, Any]], model: str, *, label: str = "",
               max_findings: int = 100) -> GateResult:
    gate = GateResult(f"schema:{label or model}")
    cls = SCHEMA_MODELS[model]
    doc = json.loads((SCHEMA_DIR / f"{model}.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(doc)
    total = bad = 0
    for n, row in enumerate(rows):
        total += 1
        errors = [f"json:{'/'.join(map(str, e.absolute_path))}: {e.message}"
                  for e in validator.iter_errors(dict(row))]
        try:
            cls.model_validate_json(json.dumps(row))
        except ValidationError as exc:
            errors += [f"model:{'/'.join(map(str, e['loc']))}: {e['msg']}"
                       for e in exc.errors()]
        if errors:
            bad += 1
            if bad <= max_findings:
                gate.error("schema_violation", "; ".join(errors[:4])[:500], f"row {n}")
    gate.stats.update(rows=total, invalid=bad)
    if total == 0:
        gate.error("empty", "no rows to validate")
    return gate


def audit_file(path: Path, model: str) -> GateResult:
    """Validate a single-object file (e.g. a manifest, label file or protocol)."""
    data = json.loads(path.read_text())
    return audit_rows([data], model, label=f"{model}:{path.name}")
