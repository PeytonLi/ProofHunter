"""Strict boundary validation of the frozen DTOs."""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from fixtures.simulate import simulate_case
from proofhunter import contracts as c


def sample(name: str) -> dict[str, Any]:
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "schemas" / "samples" / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def rejects(model: type[BaseModel], data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize("bad", [True, 1.0, "10", None, -1, 101])
def test_discount_is_strict_int_in_range(bad: object) -> None:
    rejects(c.CandidateInput, {"prices_cents": [1], "discount_percent": bad})


@pytest.mark.parametrize("prices", [[True], [1.0], ["1"], [10_001], [-1], [1] * 9])
def test_prices_are_strict_bounded(prices: list[object]) -> None:
    rejects(c.CandidateInput, {"prices_cents": prices, "discount_percent": 0})


def test_inputs_forbid_extra_keys() -> None:
    rejects(c.CandidateInput, {"prices_cents": [], "discount_percent": 0, "extra": 1})
    rejects(c.RunRequest, {"pr_url": "https://github.com/a/b/pull/1", "command": "x"})


def test_run_request_bounds() -> None:
    c.RunRequest(pr_url="https://github.com/a/b/pull/1")
    rejects(c.RunRequest, {"pr_url": "x" * 513})
    rejects(c.RunRequest, {"pr_url": "u", "seed": 2**32})
    rejects(c.RunRequest, {"pr_url": "u", "seed": True})


@pytest.mark.parametrize("owner", ["..", ".", "a/b", "ä", "", "a" * 101, "a b"])
def test_pr_ref_segments(owner: str) -> None:
    rejects(c.PrRef, {"owner": owner, "repo": "r", "number": 1})


def test_pr_number_positive_and_bounded() -> None:
    rejects(c.PrRef, {"owner": "o", "repo": "r", "number": 0})
    rejects(c.PrRef, {"owner": "o", "repo": "r", "number": 2**31})


def test_timestamps_must_be_utc() -> None:
    data = sample("RunLease")
    c.RunLease.model_validate(data)
    rejects(c.RunLease, {**data, "expires_at": "2026-11-07T19:00:00+01:00"})
    rejects(c.RunLease, {**data, "expires_at": "2026-11-07T19:00:00"})
    rejects(c.RunLease, {**data, "expires_at": 1794078900})
    lease = c.RunLease.model_validate({**data, "expires_at": datetime(2026, 1, 1, tzinfo=UTC)})
    assert lease.model_dump(mode="json")["expires_at"] == "2026-01-01T00:00:00.000000Z"
    with pytest.raises(ValidationError):
        c.RunLease.model_validate({**data, "expires_at": datetime(
            2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))})


def test_ids_are_lowercase_uuid4_and_hex() -> None:
    data = sample("RunLease")
    rejects(c.RunLease, {**data, "run_id": data["run_id"].upper()})
    rejects(c.RunLease, {**data, "run_id": "00000000-0000-1000-8000-000000000000"})
    obs = sample("SandboxObservation")
    rejects(c.SandboxObservation, {**obs, "commit_sha": obs["commit_sha"].upper()})
    rejects(c.SandboxObservation, {**obs, "image_digest": "latest"})


def test_measurements_finite_and_rounded() -> None:
    data = sample("Belief")
    for bad in (float("nan"), float("inf"), True, "0.5"):
        probs = dict(data["probabilities"], H0=bad)
        rejects(c.Belief, {**data, "probabilities": probs})
    belief = c.Belief.model_validate(data)
    assert all(round(v, 12) == v for v in belief.probabilities.values())


def test_probability_vectors_normalized_and_keyed() -> None:
    data = sample("Belief")
    rejects(c.Belief, {**data, "probabilities": {"H0": 0.5, "other": 0.4}})
    rejects(c.Belief, {**data, "probabilities": {"H0": 0.5, "unknown": 0.5}})
    rejects(c.Belief, {**data, "probabilities": {"H0": 1.0}})


def test_likelihoods_strictly_interior() -> None:
    data = sample("ProbeSelection")
    rejects(c.ProbeSelection, {**data, "likelihood_mismatch": dict(
        data["likelihood_mismatch"], H0=0.0)})


def test_plan_bounds() -> None:
    payload = sample("HypothesisPlanPayload")
    proposal = payload["hypotheses"][0]
    rejects(c.HypothesisPlanPayload, {"hypotheses": [proposal] * 7})
    rejects(c.HypothesisProposal, {**proposal, "rationale": "x" * 301})
    rejects(c.HypothesisProposal, {**proposal, "mode": "H0"})
    rejects(c.HypothesisProposal, {**proposal, "source_refs": [{"path": "../x", "line": 1}]})
    rejects(c.HypothesisProposal, {**proposal, "source_refs": [{"path": "/etc", "line": 1}]})
    rejects(c.HypothesisProposal, {**proposal, "source_refs": [{"path": "a", "line": 0}]})
    rejects(c.HypothesisProposal, {**proposal, "source_refs": [{"path": "a", "line": 1}] * 5})
    rejects(c.PlanConstraints, {"min_items": 3, "max_items": 2, "discount_percent": None,
                                "require_fractional_cent": None})
    rejects(c.PlanConstraints, {"min_items": 9, "max_items": None, "discount_percent": None,
                                "require_fractional_cent": None})
    with pytest.raises(ValidationError):  # nullable fields are required, never implicit
        c.PlanConstraints.model_validate({"min_items": None})


def test_planning_context_bounded_and_refs() -> None:
    data = sample("PlanningContext")
    context = c.PlanningContext.model_validate(data)
    assert context.contains_ref(c.SourceRef(path="checkout.py", line=1))
    assert not context.contains_ref(c.SourceRef(path="checkout.py", line=999))
    assert not context.contains_ref(c.SourceRef(path="other.py", line=1))
    rejects(c.PlanningContext, {**data, "diff": "x" * 32_000})


def test_route_decision_agreement() -> None:
    data = sample("RouteDecision")
    c.RouteDecision.model_validate(data)
    rejects(c.RouteDecision, {**data, "eligible": ["boundary"]})
    rejects(c.RouteDecision, {**data, "probabilities": {"boundary": 0.5, "constraint": 0.6}})
    rejects(c.RouteDecision, {**data, "eligible": ["constraint", "constraint"]})
    rejects(c.RouteDecision, {**data, "model_id": None})
    rejects(c.RouteDecision, {**data, "mode": "fallback", "model_id": None})


def test_observation_value_only_on_ok_and_int64() -> None:
    data = sample("SandboxObservation")
    rejects(c.SandboxObservation, {**data, "outcome": "timeout"})
    rejects(c.SandboxObservation, {**data, "value": None})
    rejects(c.SandboxObservation, {**data, "value": 2**63})
    rejects(c.SandboxObservation, {**data, "value": 42.0})
    c.SandboxObservation.model_validate({**data, "value": -5})
    c.SandboxObservation.model_validate({**data, "outcome": "invalid", "value": None})


def test_oracle_expected_only_on_ok() -> None:
    data = sample("OracleResult")
    rejects(c.OracleResult, {**data, "status": "unavailable"})
    c.OracleResult.model_validate({**data, "status": "unavailable", "expected_cents": None})


def test_execution_request_has_no_expected_and_binds_hashes() -> None:
    data = sample("ExecutionRequest")
    assert "expected_cents" not in json.dumps(c.ExecutionRequest.model_json_schema())
    rejects(c.ExecutionRequest, {**data, "expected_cents": 41})
    rejects(c.ExecutionRequest, {**data, "source_content": data["source_content"] + "#"})
    rejects(c.ExecutionRequest, {**data, "input": {"prices_cents": [1],
                                                   "discount_percent": 10}})


def test_candidate_hash_and_tags() -> None:
    data = sample("Candidate")
    rejects(c.Candidate, {**data, "input_sha256": "0" * 64})
    rejects(c.Candidate, {**data, "coverage_tags": list(reversed(data["coverage_tags"]))})


def test_source_file_verified() -> None:
    bundle = sample("SourceBundle")
    rejects(c.SourceFile, {**bundle["base"], "content": bundle["base"]["content"] + " "})
    rejects(c.SourceBundle, {**bundle, "base": {**bundle["head"]}})


def test_status_terminal_consistency() -> None:
    data = sample("RunStatus")
    rejects(c.RunStatus, {**data, "verdict": "no_defect_found"})
    rejects(c.RunStatus, {**data, "status": "completed"})
    rejects(c.RunStatus, {**data, "status": "failed", "verdict": "inconclusive"})


def test_budget_never_exceeds_limits() -> None:
    data = sample("Budget")
    rejects(c.Budget, {**data, "used": {**data["used"], "pairs": 7}})
    rejects(c.Budget, {**data, "limits": {**data["limits"], "max_pairs": 1},
                       "used": {**data["used"], "pairs": 2}})
    rejects(c.BudgetLimits, {"deadline_seconds": 181})
    rejects(c.BudgetLimits, {"max_sandbox_invocations": 16})


def test_cost_measured_or_unknown() -> None:
    rejects(c.CostRecord, {"status": "unknown", "amount_usd_micros": 5})
    rejects(c.CostRecord, {"status": "measured", "amount_usd_micros": None})


def test_coverage_missing_is_difference() -> None:
    data = sample("Coverage")
    rejects(c.Coverage, {**data, "missing": ["empty"]})


def test_run_event_transitions() -> None:
    data = sample("RunEvent")
    rejects(c.RunEvent, {**data, "from_state": "completed", "to_state": "testing"})
    rejects(c.RunEvent, {**data, "from_state": "queued", "to_state": "testing"})
    c.RunEvent.model_validate({**data, "from_state": "testing", "to_state": "completed"})


def test_quota_decision_consistency() -> None:
    rejects(c.QuotaDecision, {"allowed": True, "reason_code": "rate_limited",
                              "retry_after_seconds": 1})
    rejects(c.QuotaDecision, {"allowed": False, "reason_code": None,
                              "retry_after_seconds": None})


def test_check_intent_live_only_and_external_id() -> None:
    data = sample("CheckIntent")
    rejects(c.CheckIntent, {**data, "execution_mode": "offline_fixture"})
    rejects(c.CheckIntent, {**data, "external_id": sample("RunLease")["run_id"][:-1] + "0"})
    assert c.check_conclusion_for(c.Verdict.CONFIRMED_REGRESSION) is c.CheckConclusion.FAILURE
    assert c.check_conclusion_for(c.Verdict.NO_DEFECT_FOUND) is c.CheckConclusion.SUCCESS
    for verdict in (c.Verdict.CONFIRMED_EXISTING_DEFECT, c.Verdict.INCONCLUSIVE, None):
        assert c.check_conclusion_for(verdict) is c.CheckConclusion.NEUTRAL


def test_verdict_wording_frozen() -> None:
    data = sample("VerdictResult")
    rejects(c.VerdictResult, {**data, "wording": "All good!"})
    rejects(c.VerdictResult, {**data, "counterexample_probe_no": None})


def test_replay_command_trusted_only() -> None:
    data = sample("Replay")
    rejects(c.Replay, {**data, "command": data["command"] + "; rm -rf /"})
    rejects(c.Replay, {**data, "command": data["command"].replace("--offline",
                                                                  "--profile live")})
    with pytest.raises(ValueError, match="UUIDv4"):
        c.build_replay_command(c.ExecutionMode.OFFLINE_FIXTURE, "$(id)", None)


def test_probe_record_bindings() -> None:
    data = sample("ProbeRecord")
    rejects(c.ProbeRecord, {**data, "posterior": None})
    rejects(c.ProbeRecord, {**data, "head": {**data["head"], "side": "base"}})
    rejects(c.ProbeRecord, {**data, "head": {**data["head"], "input_sha256": "1" * 64}})
    rejects(c.ProbeRecord, {**data, "confirmation": {**data["confirmation"],
                                                     "attempt_id": data["head"]["attempt_id"]}})
    rejects(c.ProbeRecord, {**data, "exclusion_reason": "timeout"})


def test_receipt_rules_and_tamper() -> None:
    data = sample("ReceiptV1")
    receipt = c.ReceiptV1.model_validate(data)
    assert receipt.receipt_sha256 == c.compute_receipt_sha256(receipt)
    assert c.verify_receipt_sha256(json.dumps(data))
    rejects(c.ReceiptV1, {**data, "seed": data["seed"] + 1})
    assert not c.verify_receipt_sha256(json.dumps({**data, "seed": data["seed"] + 1}))
    assert not c.verify_receipt_sha256(b"[]")
    assert not c.verify_receipt_sha256(b"not json")
    unsealed = {**data, "receipt_sha256": None}
    rejects(c.ReceiptV1, {**unsealed, "replay": None})
    rejects(c.ReceiptV1, {**unsealed, "pinned_revision": None})
    rejects(c.ReceiptV1, {**unsealed, "run_kind": "replay"})
    rejects(c.ReceiptV1, {**unsealed, "providers": {
        **data["providers"], "meta": {**data["providers"]["meta"], "mode": "live"}}})


def test_public_projection_has_no_operator_identifiers() -> None:
    def keys(node: object) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            found |= set(node.get("properties", {}))
            for value in node.values():
                found |= keys(value)
        elif isinstance(node, list):
            for value in node:
                found |= keys(value)
        return found

    fields = keys(c.ReceiptV1.model_json_schema())
    for forbidden in ("installation_id", "sandbox_id", "provider_request_id", "source_content",
                      "content", "contract_text", "diff", "stdout", "stderr", "api_key",
                      "token", "rationale_prompt", "messages"):
        assert forbidden not in fields, forbidden
    probe = simulate_case("buggy").probes[0]
    with_ids = probe.model_copy(update={
        "head": probe.head.model_copy(update={"sandbox_id": "sbx-operator"})})
    public = c.public_probe(with_ids).model_dump(mode="json")
    assert "sandbox_id" not in json.dumps(public)
    revision = c.PinnedRevision.model_validate(sample("PinnedRevision"))
    assert "installation_id" not in c.public_revision(revision).model_dump()


def test_models_are_frozen() -> None:
    lease = c.RunLease.model_validate(sample("RunLease"))
    with pytest.raises(ValidationError):
        lease.epoch = 2  # type: ignore[misc]


def test_enrollment_allowlist() -> None:
    data = sample("Enrollment")
    for key, bad in (("source_path", "setup.py"), ("entrypoint", "os.system"),
                     ("contract_id", "x@1"), ("visibility", "private"),
                     ("repo_full_name", "a/b/c")):
        rejects(c.Enrollment, {**data, key: bad})
    enrollment = c.Enrollment.model_validate(data)
    other = enrollment.model_copy(update={"installation_id": 7})
    assert c.enrollment_fingerprint(enrollment) == c.enrollment_fingerprint(other)


def test_error_envelope_shape() -> None:
    data = sample("ErrorEnvelope")
    assert set(data) == {"error"}
    assert set(data["error"]) == {"code", "message", "request_id", "retryable"}
    rejects(c.ErrorEnvelope, {"error": {**data["error"], "code": "boom"}})
    normative = {"invalid_pr_url", "repo_not_enrolled", "fork_unsupported",
                 "revision_unavailable", "oracle_unavailable", "isolation_unverified",
                 "provider_unavailable", "budget_exhausted", "nondeterministic",
                 "coverage_insufficient", "stale_event", "conflicting_idempotency_key",
                 "internal_error"}
    assert normative <= {r.value for r in c.ReasonCode}
