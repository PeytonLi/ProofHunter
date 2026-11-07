"""Track B (SWE-smith) execution adapter: design only, hard-refuses every run.

This module deliberately imports no process, network or container API. A future
adapter may only run ``FIXED_RUNNER`` with one allowlisted ``TestCandidate.test_id``
after every ``ISOLATION_PREREQUISITES`` item has recorded, reviewed evidence and
setup has issued ``research-protocol-v2`` (RESEARCH-SPEC section 4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from research.schemas.models import SweSmithTask

STATUS: Final = "blocked_no_qualified_sandbox"
FIXED_RUNNER: Final = ("python", "-m", "pytest", "-x", "-p", "no:cacheprovider", "--")

#: Every item needs recorded qualification evidence before execution may be enabled.
ISOLATION_PREREQUISITES: Final[tuple[tuple[str, str], ...]] = (
    ("network_egress_denied", "default-deny egress; only a reviewed package-mirror allowlist "
     "during dependency preparation, none during test execution"),
    ("metadata_endpoint_denied", "169.254.169.254, fd00:ec2::254 and provider metadata hosts "
     "unreachable from the test process (strict qualification; Freestyle failed this)"),
    ("no_credentials", "no tokens, keys, cloud credentials, SSH agents or git credentials in "
     "env, files or mounts; verified by an in-sandbox scan before each run"),
    ("pinned_environment", "image digest, Python version, dependency lock and repo commit "
     "pinned and hashed; dependencies installed by trusted tooling before target code exists"),
    ("fresh_instance_per_run", "one disposable instance per (task, revision, test id); no reuse "
     "across tasks"),
    ("resource_limits", "enforced CPU, memory, process count, disk and wall-clock limits with "
     "bounded stdout/stderr"),
    ("fixed_command", "only FIXED_RUNNER plus one allowlisted test id; no free-form commands"),
    ("cleanup_evidence", "verified teardown: instance deleted and absent from the provider "
     "listing, recorded in the ledger (teardown_verified=true)"),
    ("reviewed_qualification", "qualification evidence recorded, reviewed and referenced by a "
     "new protocol version replacing executed=false / blocked_no_qualified_sandbox"),
)


class TrackBExecutionBlocked(PermissionError):
    """Raised for every Track B execution request under research-protocol-v1."""


@dataclass(frozen=True)
class TrackBRefusal:
    instance_id: str
    test_ids: tuple[str, ...]
    executed: Literal[False]
    execution_status: Literal["blocked_no_qualified_sandbox"]
    missing_prerequisites: tuple[str, ...]


class TrackBAdapter:
    """Interface a qualified Track B runner would implement; here it only refuses."""

    def plan(self, task: SweSmithTask, test_ids: Sequence[str]) -> TrackBRefusal:
        """Validate ``test_ids`` against the task's allowlist and describe the blocked run."""
        allowed = {t.test_id for t in task.candidate_tests}
        unknown = [t for t in test_ids if t not in allowed]
        if unknown or not test_ids:
            raise ValueError("test ids must be a non-empty subset of the task's candidate_tests")
        return TrackBRefusal(task.instance_id, tuple(test_ids), False, STATUS,
                             tuple(name for name, _ in ISOLATION_PREREQUISITES))

    def run(self, task: SweSmithTask, test_ids: Sequence[str]) -> None:
        refusal = self.plan(task, test_ids)
        raise TrackBExecutionBlocked(
            f"Track B execution is blocked ({STATUS}) for {refusal.instance_id}; missing: "
            + ", ".join(refusal.missing_prerequisites))
