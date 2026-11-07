"""Write or check the frozen JSON schema files: ``python -m research.schemas.export [--check]``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from research.schemas.models import SCHEMA_MODELS

SCHEMA_DIR = Path(__file__).resolve().parent


def render(name: str) -> str:
    schema = SCHEMA_MODELS[name].model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://proofhunter.research/schemas/v1/{name}.schema.json"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for name in SCHEMA_MODELS:
        path = SCHEMA_DIR / f"{name}.schema.json"
        text = render(name)
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(path.name)
        else:
            path.write_text(text)
    if stale:
        print("stale research schemas: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
