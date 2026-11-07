"""Frozen JSON Schema snapshots and one validated sample per boundary."""

import json
from pathlib import Path

import pytest

from fixtures import build
from proofhunter import contracts as c

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
MODELS = {model.__name__: model for model in build.SCHEMA_MODELS}


def test_schema_version_frozen() -> None:
    assert c.SCHEMA_VERSION == "1.0"


def test_snapshots_and_fixtures_have_not_drifted() -> None:
    produced = build.outputs()
    drift = [str(p) for p, text in produced.items()
             if not p.exists() or p.read_text(encoding="utf-8") != text]
    assert drift == []


@pytest.mark.parametrize("name", sorted(MODELS))
def test_schema_snapshot_matches_model(name: str) -> None:
    snapshot = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    assert snapshot == MODELS[name].model_json_schema(mode="validation")


@pytest.mark.parametrize("name", sorted(MODELS))
def test_sample_validates_and_round_trips(name: str) -> None:
    text = (SCHEMAS / "samples" / f"{name}.json").read_text(encoding="utf-8")
    model = MODELS[name].model_validate_json(text)
    again = MODELS[name].model_validate_json(model.canonical_bytes())
    assert again == model
    assert again.canonical_bytes() == model.canonical_bytes()
    assert json.loads(model.canonical_bytes()) == json.loads(text)


def test_every_sample_has_a_schema() -> None:
    samples = {p.name.removesuffix(".json") for p in (SCHEMAS / "samples").glob("*.json")}
    schemas = {p.name.removesuffix(".schema.json") for p in SCHEMAS.glob("*.schema.json")}
    assert samples == schemas == set(MODELS)
