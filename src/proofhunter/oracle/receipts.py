"""Receipt sealing, verification and replay provenance (shared serializer only).

``receipt_sha256`` binds the canonical public receipt bytes
(``ph-canonical-json-v1``) as stored by ProofHunter. Untrusted receipt JSON is
parsed strictly (UTF-8, no duplicate keys, no NaN/Infinity, bounded size) before
any hash is trusted, and every sealed or verified receipt must agree with its
own recomputed evidence.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final, NoReturn

from pydantic import ValidationError

from proofhunter.contracts import (
    ConfirmationStatus,
    Enrollment,
    EvidenceKind,
    ExecutionMode,
    PinnedRevision,
    ProbeRecord,
    PublicProbeRecord,
    PublicRevision,
    ReasonCode,
    ReceiptV1,
    Replay,
    RunnerMode,
    Side,
    SourceHashes,
    Verdict,
    build_replay_command,
    canonical_sha256,
    compute_receipt_sha256,
    verify_receipt_sha256,
)
from proofhunter.oracle.evidence import Observation, ProvenanceBinding, provenance_ok
from proofhunter.oracle.verdict import (
    MIN_CLEAN_PAIRS,
    EvidenceInconsistency,
    _coverage,
    audit_probes,
)
from proofhunter.ports import AdapterError

MAX_RECEIPT_BYTES: Final = 1024 * 1024
_PROVIDER: Final = "oracle"
AnyProbe = ProbeRecord | PublicProbeRecord


def _fail(code: ReasonCode, message: str) -> NoReturn:
    raise AdapterError(_PROVIDER, code, message, retryable=False)


def runner_mode_for(execution_mode: ExecutionMode) -> RunnerMode:
    return (RunnerMode.OFFLINE_FIXTURE if execution_mode is ExecutionMode.OFFLINE_FIXTURE
            else RunnerMode.DAYTONA)


# ---------------------------------------------------------------- replay binding


def replay_binding(replay: Replay) -> ProvenanceBinding:
    """Provenance every fresh replay observation must match."""
    return ProvenanceBinding(
        base_sha=replay.base_sha, head_sha=replay.head_sha,
        base_source_sha256=replay.base_source_sha256,
        head_source_sha256=replay.head_source_sha256, snapshot_id=replay.snapshot_id,
        image_digest=replay.image_digest, harness_sha256=replay.harness_sha256,
        runner_mode=runner_mode_for(replay.execution_mode))


def check_replay_observation(replay: Replay, observation: Observation, side: Side) -> bool:
    """True when a fresh replay observation binds the replay input and provenance."""
    return provenance_ok(observation, side, replay.input_sha256, replay_binding(replay))


def _check_probe_binding(probe: AnyProbe, *, base_sha: str, head_sha: str,
                         sources: SourceHashes, execution_mode: ExecutionMode,
                         snapshot_id: str | None = None, image_digest: str | None = None,
                         harness_sha256: str | None = None) -> None:
    binding = ProvenanceBinding(
        base_sha=base_sha, head_sha=head_sha, base_source_sha256=sources.base,
        head_source_sha256=sources.head,
        snapshot_id=probe.head.snapshot_id if snapshot_id is None else snapshot_id,
        image_digest=probe.head.image_digest if image_digest is None else image_digest,
        harness_sha256=probe.head.harness_sha256 if harness_sha256 is None else harness_sha256,
        runner_mode=runner_mode_for(execution_mode))
    digest = probe.candidate.input_sha256
    if probe.oracle.input_sha256 != digest:
        _fail(ReasonCode.TRUST_FAILURE, "oracle result does not bind the probe input")
    checks = [(probe.base, Side.BASE), (probe.head, Side.HEAD)]
    if probe.confirmation is not None:
        checks.append((probe.confirmation, Side.CONFIRM))
    for observation, side in checks:
        if not provenance_ok(observation, side, digest, binding):
            _fail(ReasonCode.TRUST_FAILURE, f"{side.value} observation provenance mismatch")


def build_replay(*, run_id: str, probe: AnyProbe,
                 revision: PinnedRevision | PublicRevision, sources: SourceHashes,
                 enrollment: Enrollment, execution_mode: ExecutionMode,
                 counterexample: bool = True) -> Replay:
    """Bind a probe's original input to the pinned SHAs, source and harness hashes,
    contract, snapshot and image, with a command built only from trusted flags."""
    if (probe.oracle.contract_id != enrollment.contract_id
            or probe.oracle.contract_sha256 != enrollment.contract_sha256
            or probe.oracle.oracle_version != enrollment.oracle_version):
        _fail(ReasonCode.TRUST_FAILURE, "probe oracle does not bind the enrollment")
    _check_probe_binding(probe, base_sha=revision.base_sha, head_sha=revision.head_sha,
                         sources=sources, execution_mode=execution_mode,
                         snapshot_id=enrollment.snapshot_id,
                         image_digest=enrollment.image_digest,
                         harness_sha256=enrollment.harness_sha256)
    return Replay(
        probe_no=probe.probe_no, input=probe.candidate.as_input(),
        input_sha256=probe.candidate.input_sha256, base_sha=revision.base_sha,
        head_sha=revision.head_sha, base_source_sha256=sources.base,
        head_source_sha256=sources.head, harness_version=enrollment.harness_version,
        harness_sha256=enrollment.harness_sha256, contract_id=enrollment.contract_id,
        contract_sha256=enrollment.contract_sha256, oracle_version=enrollment.oracle_version,
        snapshot_id=enrollment.snapshot_id, image_digest=enrollment.image_digest,
        execution_mode=execution_mode,
        command=build_replay_command(execution_mode, run_id,
                                     None if counterexample else probe.probe_no))


def _check_replay_against(receipt: ReceiptV1, replay: Replay) -> AnyProbe:
    revision, sources = receipt.pinned_revision, receipt.source_file_sha256
    if revision is None or sources is None:
        _fail(ReasonCode.REPLAY_UNAVAILABLE, "receipt has no pinned revision")
    if not 1 <= replay.probe_no <= len(receipt.probes):
        _fail(ReasonCode.REPLAY_UNAVAILABLE, "replay probe is not in the receipt")
    probe = receipt.probes[replay.probe_no - 1]
    candidate = probe.candidate
    if (replay.input_sha256 != candidate.input_sha256
            or list(replay.input.prices_cents) != list(candidate.prices_cents)
            or replay.input.discount_percent != candidate.discount_percent):
        _fail(ReasonCode.TRUST_FAILURE, "replay input does not bind the probe input")
    if (replay.base_sha, replay.head_sha) != (revision.base_sha, revision.head_sha) or (
            replay.base_source_sha256, replay.head_source_sha256) != (sources.base, sources.head):
        _fail(ReasonCode.TRUST_FAILURE, "replay does not bind the pinned revision and sources")
    if replay.execution_mode is not receipt.execution_mode:
        _fail(ReasonCode.TRUST_FAILURE, "replay mode does not match the receipt")
    if (replay.contract_id != probe.oracle.contract_id
            or replay.contract_sha256 != probe.oracle.contract_sha256
            or replay.oracle_version != probe.oracle.oracle_version
            or replay.oracle_version != receipt.policy_versions.oracle_version
            or replay.harness_version != receipt.policy_versions.harness_version):
        _fail(ReasonCode.TRUST_FAILURE, "replay contract or oracle binding mismatch")
    allowed = {build_replay_command(replay.execution_mode, receipt.run_id, None),
               build_replay_command(replay.execution_mode, receipt.run_id, replay.probe_no)}
    if replay.command not in allowed:
        _fail(ReasonCode.TRUST_FAILURE, "replay command is not bound to this receipt")
    _check_probe_binding(probe, base_sha=revision.base_sha, head_sha=revision.head_sha,
                         sources=sources, execution_mode=receipt.execution_mode,
                         snapshot_id=replay.snapshot_id, image_digest=replay.image_digest,
                         harness_sha256=replay.harness_sha256)
    return probe


# ---------------------------------------------------------------- receipt semantics


def check_receipt_semantics(receipt: ReceiptV1) -> None:
    """Raise ``AdapterError(trust_failure)`` unless the receipt agrees with its evidence."""
    try:
        audited = audit_probes(receipt.probes)
    except EvidenceInconsistency as error:
        _fail(ReasonCode.TRUST_FAILURE, str(error))
    if _coverage(audited, receipt.coverage.coverage_guard_used) != receipt.coverage:
        _fail(ReasonCode.TRUST_FAILURE, "coverage does not match the probe evidence")
    revision, sources = receipt.pinned_revision, receipt.source_file_sha256
    if receipt.probes and (revision is None or sources is None):
        _fail(ReasonCode.TRUST_FAILURE, "probes require a pinned revision and source hashes")
    if revision is not None and sources is not None:
        for probe in receipt.probes:
            _check_probe_binding(probe, base_sha=revision.base_sha, head_sha=revision.head_sha,
                                 sources=sources, execution_mode=receipt.execution_mode)
    for probe in receipt.probes:
        if probe.oracle.oracle_version != receipt.policy_versions.oracle_version:
            _fail(ReasonCode.TRUST_FAILURE, "oracle version differs from policy_versions")
    verdict = receipt.verdict
    if verdict in (Verdict.CONFIRMED_REGRESSION, Verdict.CONFIRMED_EXISTING_DEFECT):
        number = receipt.counterexample_probe_no
        if number is None or number > len(audited):
            _fail(ReasonCode.TRUST_FAILURE, "counterexample probe is missing")
        item = audited[number - 1]
        wanted = (EvidenceKind.HEAD_MISMATCH if verdict is Verdict.CONFIRMED_REGRESSION
                  else EvidenceKind.SHARED_MISMATCH)
        if (item.duplicate or item.assessment.evidence_kind is not wanted
                or item.assessment.confirmation_status is not ConfirmationStatus.REPRODUCED):
            _fail(ReasonCode.TRUST_FAILURE, "confirmed verdict lacks a reproduced chain")
        if receipt.reason_code is not ReasonCode.CONFIRMED_EVIDENCE:
            _fail(ReasonCode.TRUST_FAILURE, "confirmed verdict requires confirmed_evidence")
    elif verdict is Verdict.NO_DEFECT_FOUND:
        live = [a for a in audited if not a.duplicate]
        if (not live or any(a.assessment.evidence_kind is not EvidenceKind.MATCH for a in live)
                or not receipt.coverage.complete
                or receipt.coverage.distinct_usable_pairs < MIN_CLEAN_PAIRS
                or receipt.reason_code is not ReasonCode.COVERAGE_COMPLETE):
            _fail(ReasonCode.TRUST_FAILURE, "no_defect_found lacks clean coverage evidence")
    if receipt.replay is not None:
        _check_replay_against(receipt, receipt.replay)


# ---------------------------------------------------------------- seal and verify


def seal(receipt: ReceiptV1) -> ReceiptV1:
    """Re-validate the public receipt, check its evidence, and set ``receipt_sha256``."""
    try:
        data = receipt.model_dump(mode="json")
        data["receipt_sha256"] = None
        unsealed = ReceiptV1.model_validate_json(json.dumps(data, allow_nan=False))
    except (ValidationError, ValueError, TypeError) as error:
        _fail(ReasonCode.INTERNAL_ERROR, f"receipt is not a valid public receipt: "
              f"{type(error).__name__}")
    check_receipt_semantics(unsealed)
    digest = compute_receipt_sha256(unsealed)
    return ReceiptV1.model_validate({**unsealed.model_dump(), "receipt_sha256": digest})


def _no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate object key")
    return dict(pairs)


def _no_constants(name: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant {name}")


def _strict_json(receipt_json: bytes | str) -> tuple[str, dict[str, Any]]:
    if isinstance(receipt_json, bytes | bytearray):
        if len(receipt_json) > MAX_RECEIPT_BYTES:
            raise ValueError("receipt too large")
        text = bytes(receipt_json).decode("utf-8", errors="strict")
    elif isinstance(receipt_json, str):
        text = receipt_json
        if len(text.encode("utf-8", errors="strict")) > MAX_RECEIPT_BYTES:
            raise ValueError("receipt too large")
    else:
        raise TypeError("receipt must be bytes or str")
    data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    if not isinstance(data, dict):
        raise ValueError("receipt must be a JSON object")
    return text, data


def verify_receipt(receipt_json: bytes | str) -> ReceiptV1:
    """Parse untrusted receipt JSON and verify its canonical hash and evidence.

    Raises ``AdapterError(receipt_tampered)`` for malformed, unsealed or mismatched
    receipts and ``AdapterError(trust_failure)`` when a correctly hashed receipt
    disagrees with its own evidence.
    """
    try:
        text, data = _strict_json(receipt_json)
        stored = data.get("receipt_sha256")
        if not isinstance(stored, str):
            raise ValueError("receipt is not sealed")
        body = {k: v for k, v in data.items() if k != "receipt_sha256"}
        receipt = ReceiptV1.model_validate_json(text)
        intact = (canonical_sha256(body) == stored
                  and compute_receipt_sha256(receipt) == stored
                  and receipt.receipt_sha256 == stored
                  and verify_receipt_sha256(text))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        intact = False
    if not intact:
        _fail(ReasonCode.RECEIPT_TAMPERED, "receipt hash does not verify")
    check_receipt_semantics(receipt)
    return receipt


def validate_replay(receipt: ReceiptV1, probe_no: int | None) -> Replay:
    """Return the replay bound to ``probe_no`` (``None`` selects the counterexample).

    The receipt must be sealed and intact. The stored replay is returned for its
    own probe after full provenance checks; any other probe yields a replay
    derived from the receipt's pinned revision, source hashes and that probe's
    observations, with a ``--probe N`` command.
    """
    if receipt.receipt_sha256 is None or receipt.receipt_sha256 != compute_receipt_sha256(
            receipt):
        _fail(ReasonCode.RECEIPT_TAMPERED, "receipt hash does not verify")
    check_receipt_semantics(receipt)
    wanted = receipt.counterexample_probe_no if probe_no is None else probe_no
    if type(wanted) is not int or not 1 <= wanted <= len(receipt.probes):
        _fail(ReasonCode.REPLAY_UNAVAILABLE, "no eligible pinned input")
    revision, sources = receipt.pinned_revision, receipt.source_file_sha256
    if revision is None or sources is None:
        _fail(ReasonCode.REPLAY_UNAVAILABLE, "no eligible pinned input")
    if receipt.replay is not None and receipt.replay.probe_no == wanted:
        _check_replay_against(receipt, receipt.replay)
        return receipt.replay
    if probe_no is None:
        _fail(ReasonCode.REPLAY_UNAVAILABLE, "receipt has no counterexample replay")
    probe = receipt.probes[wanted - 1]
    head = probe.head
    replay = Replay(
        probe_no=wanted, input=probe.candidate.as_input(),
        input_sha256=probe.candidate.input_sha256, base_sha=revision.base_sha,
        head_sha=revision.head_sha, base_source_sha256=sources.base,
        head_source_sha256=sources.head,
        harness_version=receipt.policy_versions.harness_version,
        harness_sha256=head.harness_sha256, contract_id=probe.oracle.contract_id,
        contract_sha256=probe.oracle.contract_sha256,
        oracle_version=probe.oracle.oracle_version, snapshot_id=head.snapshot_id,
        image_digest=head.image_digest, execution_mode=receipt.execution_mode,
        command=build_replay_command(receipt.execution_mode, receipt.run_id, wanted))
    _check_replay_against(receipt, replay)
    return replay
