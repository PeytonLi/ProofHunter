from __future__ import annotations

import ast
from pathlib import Path

import pytest

from research.execution import track_b
from research.schemas.models import SweSmithTask

SAMPLE = Path(__file__).resolve().parents[3] / "research/schemas/samples/SweSmithTask.json"


@pytest.fixture
def task() -> SweSmithTask:
    return SweSmithTask.model_validate_json(SAMPLE.read_text())


def test_run_always_refuses(task):
    ids = [t.test_id for t in task.candidate_tests][:1]
    with pytest.raises(track_b.TrackBExecutionBlocked, match="blocked_no_qualified_sandbox"):
        track_b.TrackBAdapter().run(task, ids)


def test_plan_states_prerequisites_and_never_executes(task):
    ids = [t.test_id for t in task.candidate_tests]
    refusal = track_b.TrackBAdapter().plan(task, ids)
    assert refusal.executed is False
    assert refusal.execution_status == task.execution_status
    for need in ("network_egress_denied", "metadata_endpoint_denied", "no_credentials",
                 "pinned_environment", "resource_limits", "cleanup_evidence"):
        assert need in refusal.missing_prerequisites


@pytest.mark.parametrize("ids", [[], ["tests/test_x.py::not_listed"]])
def test_non_allowlisted_test_ids_rejected(task, ids):
    with pytest.raises(ValueError):
        track_b.TrackBAdapter().plan(task, ids)


def test_module_has_no_execution_capability():
    tree = ast.parse(Path(track_b.__file__).read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.Import | ast.ImportFrom) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"subprocess", "os", "socket", "shutil", "multiprocessing", "pty",
                           "importlib", "ctypes", "docker", "requests", "httpx"}
