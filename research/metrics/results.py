"""Raw result writer/reader for frozen ``RawResultRow`` episodes (JSONL canonical, CSV flat)."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from research.schemas.models import LabelFile, ProtocolV1, RawResultRow


class ResultsError(ValueError):
    pass


def validate_row(row: RawResultRow) -> RawResultRow:
    return RawResultRow.model_validate_json(row.model_dump_json())


def write_raw_results(path: Path, rows: Iterable[RawResultRow]) -> tuple[Path, Path]:
    checked = [validate_row(r) for r in rows]
    ids = [r.run_id for r in checked]
    if len(set(ids)) != len(ids):
        raise ResultsError("duplicate run_id")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.model_dump_json() + "\n" for r in checked), encoding="utf-8")
    csv_path = path.with_suffix(".csv")
    flat = [_flatten(r.model_dump(mode="json")) for r in checked]
    fields = sorted({k for row in flat for k in row}) if flat else []
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(flat)
    return path, csv_path


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{name}."))
        elif isinstance(value, list):
            out[name] = json.dumps(value, sort_keys=True)
        else:
            out[name] = value
    return out


def read_raw_results(*paths: Path) -> list[RawResultRow]:
    rows: list[RawResultRow] = []
    seen: set[str] = set()
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = RawResultRow.model_validate_json(line)
            except ValueError as error:
                raise ResultsError(f"{path}:{number}: invalid result row: {error}") from error
            if row.run_id in seen:
                raise ResultsError(f"{path}:{number}: duplicate run_id {row.run_id!r}")
            seen.add(row.run_id)
            rows.append(row)
    return rows


def read_label_files(paths: Sequence[Path], *, allow_sealed: bool = False) -> list[LabelFile]:
    files = []
    for path in paths:
        label_file = LabelFile.model_validate_json(path.read_text(encoding="utf-8"))
        if label_file.sealed and not allow_sealed:
            raise ResultsError(f"{path}: sealed test labels need an explicit unseal flag")
        files.append(label_file)
    return files


DEGRADED_REASON = re.compile(r"^[a-z0-9_:]{1,64}$")


def read_degraded(*paths: Path) -> dict[str, str]:
    """Read ``{"run_id": ..., "degraded": reason | null}`` JSONL records, one per episode.

    ``RawResultRow`` has no degraded field, so arms that fell back to fixed order report it here.
    """
    out: dict[str, str] = {}
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            where = f"{path}:{number}"
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ResultsError(f"{where}: invalid JSON: {error}") from error
            if not isinstance(record, dict) or not isinstance(record.get("run_id"), str):
                raise ResultsError(f"{where}: degradation record needs a string run_id")
            reason = record.get("degraded")
            if reason is None:
                continue
            if not isinstance(reason, str) or not DEGRADED_REASON.match(reason):
                raise ResultsError(f"{where}: degraded must be null or a reason code")
            run_id = record["run_id"]
            if out.get(run_id, reason) != reason:
                raise ResultsError(f"{where}: conflicting degraded reasons for {run_id!r}")
            out[run_id] = reason
    return out


def read_protocol(path: Path) -> ProtocolV1:
    return ProtocolV1.model_validate_json(path.read_text(encoding="utf-8"))
