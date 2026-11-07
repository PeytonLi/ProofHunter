"""Frozen provider configuration (``models.json``)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

MODELS_PATH = Path(__file__).with_name("models.json")


@cache
def load_models() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    return data


def frontier_model_ids() -> tuple[str, ...]:
    f = load_models()["frontier"]
    return (f["primary_model_id"], f["optional_model_id"])
