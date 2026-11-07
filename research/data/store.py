"""Directory-backed ``CaseStore`` with a sealed test-label file."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from research.schemas import interfaces as I
from research.schemas import models as M

TEST_LABELS = "labels/test.sealed.json"


class SealedLabelsError(PermissionError):
    pass


def unseal_token(label_file_sha256: str) -> str:
    """The unseal token is bound to the exact sealed file: ``research-protocol-v1:<sha256>``."""
    return f"research-protocol-v1:{label_file_sha256}"


class DirectoryCaseStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._manifest: M.CaseManifest | None = None

    def manifest(self) -> M.CaseManifest:
        if self._manifest is None:
            data = (self.root / "case-manifest.json").read_bytes()
            self._manifest = M.CaseManifest.model_validate_json(data)
        return self._manifest

    def sources(self, case_id: str) -> I.CaseSources:
        case = next((c for c in self.manifest().cases if c.case_id == case_id), None)
        if case is None:
            raise KeyError(case_id)
        out = []
        for digest in (case.base_source_sha256, case.head_source_sha256):
            data = (self.root / "sources" / f"{digest}.py").read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"source hash mismatch for {case_id}")
            out.append(data)
        return I.CaseSources(case=case, base_source=out[0], head_source=out[1])

    def labels(self, split: str, *, unseal: str | None = None) -> Sequence[M.CaseLabel]:
        target = M.Split(split)
        if target is M.Split.TEST:
            path = self.root / TEST_LABELS
            raw = path.read_bytes()
            if unseal != unseal_token(hashlib.sha256(raw).hexdigest()):
                raise SealedLabelsError("test labels are sealed until frozen evaluation")
        else:
            raw = (self.root / "labels" / f"{target.value}.json").read_bytes()
        lf = M.LabelFile.model_validate_json(raw)
        if lf.split is not target or lf.manifest_cases_sha256 != self.manifest().cases_sha256:
            raise ValueError("label file does not match the manifest")
        return list(lf.labels)


def open_store(root: Path) -> I.CaseStore:
    return DirectoryCaseStore(root)
