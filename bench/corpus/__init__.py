"""Frozen corpus validation and label-free execution inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from fixtures import catalog
from proofhunter.contracts import SourceFile, canonical_sha256, git_blob_sha1, sha256_hex

ROOT = Path(__file__).parent


def oracle(prices_cents: list[int], discount_percent: int) -> int:
    return (sum(prices_cents) * (100 - discount_percent) + 50) // 100


class Witness(BaseModel):
    prices_cents: list[int]
    discount_percent: int
    expected: int
    head: int


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    split: Literal["dev", "holdout"]
    template_group: str
    base_path: str
    head_path: str
    base_revision: str
    head_revision: str
    base_sha256: str
    head_sha256: str
    contract_id: Literal["checkout-rounding@1"] = "checkout-rounding@1"
    contract_sha256: str
    label: Literal["defective", "clean"]
    expected_verdict: str
    defect_family: str
    witness: Witness | None = None
    review_rationale: str | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    manifest_sha256: str
    sample: list[str]
    cases: list[Case]


def load(root: Path = ROOT) -> Manifest:
    manifest = Manifest.model_validate_json((root / "manifest.json").read_text())
    payload = manifest.model_dump(exclude={"manifest_sha256"})
    if canonical_sha256(payload) != manifest.manifest_sha256:
        raise ValueError("manifest hash drift")
    if len({c.case_id for c in manifest.cases}) != len(manifest.cases):
        raise ValueError("duplicate case id")
    if not set(manifest.sample) <= {c.case_id for c in manifest.cases}:
        raise ValueError("unknown sample id")
    for case in manifest.cases:
        if case.contract_sha256 != catalog.contract_sha256():
            raise ValueError("contract drift")
        for side in ("base", "head"):
            path = (root / getattr(case, f"{side}_path")).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("source outside corpus")
            if sha256_hex(path.read_bytes()) != getattr(case, f"{side}_sha256"):
                raise ValueError("source hash drift")
        if case.label == "defective":
            w = case.witness
            if w is None or oracle(w.prices_cents, w.discount_percent) != w.expected:
                raise ValueError("invalid independent witness")
            if w.head == w.expected:
                raise ValueError("witness must distinguish defect")
        elif not case.review_rationale:
            raise ValueError("clean case needs review rationale")
    return manifest


def sources(case: Case, root: Path) -> tuple[SourceFile, SourceFile]:
    def source(side: Literal["base", "head"]) -> SourceFile:
        data = (root / getattr(case, f"{side}_path")).read_bytes()
        return SourceFile(side=side, commit_sha=getattr(case, f"{side}_revision"),
                          path="checkout.py", blob_sha=git_blob_sha1(data),
                          content=data.decode(), content_sha256=sha256_hex(data),
                          size_bytes=len(data))
    return source("base"), source("head")


def revision(name: str) -> str:
    return hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest()


def check() -> int:
    manifest = load()
    print(json.dumps({"version": manifest.version, "cases": len(manifest.cases),
                      "manifest_sha256": manifest.manifest_sha256}))
    return 0
