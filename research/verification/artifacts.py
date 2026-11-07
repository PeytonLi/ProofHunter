"""Artifact loading and hashing helpers (read-only)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    """Relative POSIX path -> sha256 for every regular file under ``root`` (sorted)."""
    if root.is_file():
        return {root.name: sha256_file(root)}
    return {p.relative_to(root).as_posix(): sha256_file(p)
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Rows from ``.jsonl`` (one object per line) or ``.json`` (list or {"rows": [...]})."""
    if path.suffix == ".jsonl":
        with path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError(f"{path}:{n}: row is not an object")
                    yield row
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("rows", "cases", "records", "results", "entries"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of rows")
    for row in data:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row is not an object")
        yield row


def load_rows(path: Path) -> list[dict[str, Any]]:
    return list(iter_rows(path))


def get_path(row: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """``a.b.c`` lookup returning ``default`` when any segment is missing."""
    cur: Any = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
