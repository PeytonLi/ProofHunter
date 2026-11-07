"""Validate externally frozen holdout without importing it into the worktree."""

from pathlib import Path

from bench.corpus import ROOT, Manifest, load


def validate(root: Path) -> Manifest:
    dev = load(ROOT)
    holdout = load(root)
    hashes = {h for c in dev.cases for h in (c.base_sha256, c.head_sha256)}
    groups = {c.template_group for c in dev.cases}
    if any(c.split != "holdout" for c in holdout.cases):
        raise ValueError("holdout split required")
    if any(c.template_group in groups or c.base_sha256 in hashes or c.head_sha256 in hashes
           for c in holdout.cases):
        raise ValueError("holdout source hash or template group overlaps dev/sample")
    defective = [c for c in holdout.cases if c.label == "defective"]
    clean = [c for c in holdout.cases if c.label == "clean"]
    if (len(holdout.cases) < 36 or len(defective) < 24 or len(clean) < 12
            or len({c.head_sha256 for c in defective}) < 24):
        raise ValueError("holdout needs >=24 distinct defective and >=12 clean cases")
    return holdout
