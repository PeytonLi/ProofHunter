from __future__ import annotations

import pytest

from fixtures.adapters import FixtureControlApi
from proofhunter.acp.render import MAX_REPLY_CHARS, render_evidence, render_queued, render_status
from proofhunter.contracts import (
    EvidenceCounts,
    ExecutionMode,
    RunKind,
    RunRequest,
    RunState,
    RunStatusDetail,
)

from .conftest import PR_URL, fixture_receipt


@pytest.mark.parametrize("mode", [None, ExecutionMode.LIVE, ExecutionMode.OFFLINE_FIXTURE])
async def test_execution_label_requires_reported_mode(mode: ExecutionMode | None) -> None:
    status = await FixtureControlApi().create_run(RunRequest(pr_url=PR_URL), "mode-label")
    if mode is not None:
        status = status.model_copy(update={"detail": RunStatusDetail(
            run_kind=RunKind.VERIFICATION, execution_mode=mode, repo_full_name=None,
            pr_number=None, base_sha=None, head_sha=None, coverage=None,
            evidence=EvidenceCounts(matches=0, head_mismatches=0, shared_mismatches=0,
                                    base_mismatches=0, excluded=0, confirmations=0),
            provider_modes={}, parent_receipt_sha256=None,
        )})
    for render in (render_queued, render_status):
        reply = render(status)
        if mode is None:
            assert "Mode: pending." in reply
            assert "offline_fixture" not in reply
            assert "Mode: live." not in reply
        else:
            assert f"Mode: {mode.value}." in reply


@pytest.mark.parametrize("case", ["buggy", "fixed"])
def test_evidence_groups_results_and_preserves_receipt_details(case: str) -> None:
    receipt = fixture_receipt(case)
    reply = render_evidence(receipt)
    assert reply.startswith(f"**Verdict: {receipt.verdict.value}**\n\n")
    assert "\n\n**Receipt details**\n\n" in reply
    assert receipt.receipt_sha256 in reply
    assert receipt.run_id in reply
    assert receipt.pinned_revision.base_sha in reply
    assert receipt.pinned_revision.head_sha in reply
    assert "Mode: offline_fixture." in reply
    assert len(reply) <= MAX_REPLY_CHARS
    if receipt.replay is not None:
        assert f"```bash\n{receipt.replay.command}\n```" in reply
        assert "\n\n**Results (cents)**\n\n- Expected:" in reply
    else:
        assert "\n\n**Coverage and limits**\n\n" in reply


@pytest.mark.parametrize("state", [RunState.QUEUED, RunState.COMPLETED, RunState.FAILED])
async def test_status_groups_summary_details_and_next_command(state: RunState) -> None:
    receipt = fixture_receipt("buggy")
    status = await FixtureControlApi().create_run(RunRequest(pr_url=PR_URL), "status-format")
    status = status.model_copy(update={
        "status": state,
        "verdict": receipt.verdict if state is RunState.COMPLETED else None,
        "receipt_sha256": receipt.receipt_sha256 if state is RunState.COMPLETED else None,
        "reason_code": receipt.reason_code if state is RunState.COMPLETED else None,
    })
    reply = render_status(status, execution_mode=ExecutionMode.LIVE)
    assert f"- Run ID: `{status.run_id}`" in reply
    assert f"- State: {state.value}" in reply
    assert len(reply) <= MAX_REPLY_CHARS
    if state is RunState.QUEUED:
        assert reply.startswith("**Run status: queued**\n\n")
        assert f"```text\n@proofhunter status {status.run_id}\n```" in reply
    else:
        assert f"```text\n@proofhunter evidence {status.run_id}\n```" in reply
        if state is RunState.COMPLETED:
            assert reply.startswith("**Verdict: confirmed_regression**\n\n")
            assert receipt.receipt_sha256 in reply
            assert "- Mode: live." in reply
        else:
            assert reply.startswith("**Run failed**\n\n")
            assert "failed before producing a verdict" in reply
