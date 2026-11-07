"""Trusted demo source snapshots, PR cases and the offline example enrollment.

The demo revisions under ``fixtures/demo/revisions`` are reviewed first-party
code. Commit SHAs and GitHub IDs here are fixture values for offline mode only.
Real demo PR URLs and commit IDs are published by the operator before live
verification (see docs/provider-smoke.md).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final

from proofhunter.contracts import (
    CONTRACT_ID,
    ENTRYPOINT,
    HARNESS_VERSION,
    ORACLE_VERSION,
    SCHEMA_VERSION,
    SOURCE_PATH,
    Enrollment,
    git_blob_sha1,
    sha256_hex,
)

ROOT: Final = Path(__file__).resolve().parent
DEMO_DIR: Final = ROOT / "demo"
REVISIONS_DIR: Final = DEMO_DIR / "revisions"
PROVIDERS_DIR: Final = ROOT / "providers"
CONTRACT_PATH: Final = DEMO_DIR / "contract.json"
HARNESS_PATH: Final = DEMO_DIR / "harness.json"

DEMO_OWNER: Final = "proofhunter-demo"
DEMO_REPO: Final = "checkout-demo"
DEMO_REPO_FULL_NAME: Final = f"{DEMO_OWNER}/{DEMO_REPO}"
DEMO_REPO_ID: Final = 900_000_001
DEMO_INSTALLATION_ID: Final = 900_000_002
OFFLINE_SNAPSHOT_ID: Final = "proofhunter-offline-fixture-v1"
OFFLINE_IMAGE_DIGEST: Final = "sha256:" + hashlib.sha256(
    b"proofhunter-offline-fixture-image-v1").hexdigest()

REVISION_LABELS: Final = ("base", "buggy", "fixed", "half_even")


def commit_sha(label: str) -> str:
    """Deterministic fixture commit SHA for a revision label."""
    return hashlib.sha1(f"proofhunter-fixture-commit:{label}".encode(),
                        usedforsecurity=False).hexdigest()


@dataclass(frozen=True, slots=True)
class Revision:
    label: str
    commit_sha: str
    path: Path

    @property
    def source_bytes(self) -> bytes:
        return self.path.read_bytes()

    @property
    def source_sha256(self) -> str:
        return sha256_hex(self.source_bytes)

    @property
    def blob_sha(self) -> str:
        return git_blob_sha1(self.source_bytes)


@cache
def revisions() -> dict[str, Revision]:
    return {label: Revision(label, commit_sha(label), REVISIONS_DIR / label / SOURCE_PATH)
            for label in REVISION_LABELS}


def revision_by_source_sha256(digest: str) -> Revision | None:
    return next((r for r in revisions().values() if r.source_sha256 == digest), None)


@dataclass(frozen=True, slots=True)
class PrCase:
    """Demo PR: ``number`` in the demo repo, base and head revision labels."""

    name: str
    number: int
    base: str
    head: str
    expected_verdict: str

    @property
    def url(self) -> str:
        return f"https://github.com/{DEMO_REPO_FULL_NAME}/pull/{self.number}"

    @property
    def base_revision(self) -> Revision:
        return revisions()[self.base]

    @property
    def head_revision(self) -> Revision:
        return revisions()[self.head]


#: Demo cases. ``existing`` pins two distinct commits with identical buggy bytes.
PR_CASES: Final = {
    "buggy": PrCase("buggy", 1, "base", "buggy", "confirmed_regression"),
    "fixed": PrCase("fixed", 2, "base", "fixed", "no_defect_found"),
    "half_even": PrCase("half_even", 3, "base", "half_even", "confirmed_regression"),
    "existing": PrCase("existing", 4, "buggy", "buggy", "confirmed_existing_defect"),
}


def case_by_number(number: int) -> PrCase | None:
    return next((c for c in PR_CASES.values() if c.number == number), None)


def contract_sha256() -> str:
    return sha256_hex(CONTRACT_PATH.read_bytes())


def harness_sha256() -> str:
    return sha256_hex(HARNESS_PATH.read_bytes())


def contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def demo_enrollment() -> Enrollment:
    """Offline example enrollment (mirrors config/enrollments.example.json)."""
    return Enrollment(
        schema_version=SCHEMA_VERSION, repo_id=DEMO_REPO_ID, repo_full_name=DEMO_REPO_FULL_NAME,
        installation_id=DEMO_INSTALLATION_ID, visibility="public", source_path=SOURCE_PATH,
        entrypoint=ENTRYPOINT, contract_id=CONTRACT_ID, contract_sha256=contract_sha256(),
        oracle_version=ORACLE_VERSION, harness_version=HARNESS_VERSION,
        harness_sha256=harness_sha256(), snapshot_id=OFFLINE_SNAPSHOT_ID,
        image_digest=OFFLINE_IMAGE_DIGEST, enabled=True,
    )
