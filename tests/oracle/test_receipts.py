from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from fixtures import catalog
from fixtures.simulate import SimulationResult
from proofhunter import oracle as oracle_pkg
from proofhunter.contracts import (
    ExecutionMode,
    ReasonCode,
    ReceiptV1,
    Side,
    SourceHashes,
    Verdict,
    build_replay_command,
    compute_receipt_sha256,
)
from proofhunter.oracle import (
    build_replay,
    check_replay_observation,
    seal,
    validate_replay,
    verify_receipt,
)
from proofhunter.ports import (
    REQUIRED_IMPLEMENTATIONS,
    AdapterError,
    Classifier,
    CoverageFn,
    Evaluator,
    ReceiptVerifier,
    ReplayValidator,
    Sealer,
)


def raises(code: ReasonCode, fn: Callable[[], object]) -> None:
    with pytest.raises(AdapterError) as info:
        fn()
    assert info.value.code is code
    assert info.value.retryable is False


def reseal_raw(receipt: ReceiptV1, **changes: Any) -> ReceiptV1:
    """Forge a receipt with a correct hash, bypassing ``seal`` semantics."""
    data = {**receipt.model_dump(), **changes, "receipt_sha256": None}
    unsealed = ReceiptV1.model_validate(data)
    return ReceiptV1.model_validate({**data, "receipt_sha256": compute_receipt_sha256(unsealed)})


def test_ports_are_satisfied() -> None:
    evaluator: Evaluator = oracle_pkg.evaluate
    classifier: Classifier = oracle_pkg.classify
    coverage: CoverageFn = oracle_pkg.compute_coverage
    sealer: Sealer = oracle_pkg.seal
    verifier: ReceiptVerifier = oracle_pkg.verify_receipt
    validator: ReplayValidator = oracle_pkg.validate_replay
    assert all((evaluator, classifier, coverage, sealer, verifier, validator))
    for name in REQUIRED_IMPLEMENTATIONS["proofhunter.oracle"]:
        assert callable(getattr(oracle_pkg, name))


def test_production_oracle_does_not_import_fixtures() -> None:
    from pathlib import Path

    package = Path(oracle_pkg.__file__).parent
    for path in package.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "fixtures" not in text, path.name


# ---------------------------------------------------------------- golden receipts


@pytest.mark.parametrize(("name", "verdict"), [("buggy", Verdict.CONFIRMED_REGRESSION),
                                               ("fixed", Verdict.NO_DEFECT_FOUND)])
def test_golden_receipts_verify_and_reseal(golden_bytes: Callable[[str], bytes], name: str,
                                           verdict: Verdict) -> None:
    raw = golden_bytes(name)
    receipt = verify_receipt(raw)
    assert receipt.verdict is verdict
    assert receipt.execution_mode is ExecutionMode.OFFLINE_FIXTURE
    assert seal(receipt).receipt_sha256 == receipt.receipt_sha256
    assert verify_receipt(raw.decode("utf-8")) == receipt


def test_seal_matches_simulated_receipts(cases: dict[str, SimulationResult]) -> None:
    for result in cases.values():
        unsealed = result.receipt.model_copy(update={"receipt_sha256": None})
        sealed = seal(unsealed)
        assert sealed.receipt_sha256 == result.receipt.receipt_sha256
        assert verify_receipt(sealed.model_dump_json()) == sealed


# ---------------------------------------------------------------- tampering


@pytest.mark.parametrize("mutate", [
    lambda d: d.__setitem__("seed", d["seed"] + 1),
    lambda d: d["probes"][0]["head"].__setitem__("value", 41),
    lambda d: d["probes"][0]["oracle"].__setitem__("expected_cents", 42),
    lambda d: d.__setitem__("verdict", "no_defect_found"),
    lambda d: d.__setitem__("receipt_sha256", "0" * 64),
    lambda d: d.__setitem__("receipt_sha256", None),
    lambda d: d.pop("receipt_sha256"),
    lambda d: d.__setitem__("extra", 1),
    lambda d: d["replay"].__setitem__("head_sha", "f" * 40),
])
def test_tampered_receipt_is_refused(golden_bytes: Callable[[str], bytes],
                                     mutate: Callable[[dict[str, Any]], None]) -> None:
    data = json.loads(golden_bytes("buggy"))
    mutate(data)
    raises(ReasonCode.RECEIPT_TAMPERED, lambda: verify_receipt(json.dumps(data)))


@pytest.mark.parametrize("raw", [
    b"\xff\xfe", b"[]", b"null", b"{", "x" * (oracle_pkg.MAX_RECEIPT_BYTES + 1),
])
def test_malformed_receipt_bytes_are_refused(raw: bytes | str) -> None:
    raises(ReasonCode.RECEIPT_TAMPERED, lambda: verify_receipt(raw))


def test_duplicate_keys_and_nan_are_refused(golden_bytes: Callable[[str], bytes]) -> None:
    text = golden_bytes("buggy").decode()
    duplicated = text.replace('"seed": 17', '"seed": 18, "seed": 17', 1)
    assert duplicated != text
    raises(ReasonCode.RECEIPT_TAMPERED, lambda: verify_receipt(duplicated))
    nan = text.replace('"eig_bits": ', '"eig_bits": NaN, "x": ', 1)
    raises(ReasonCode.RECEIPT_TAMPERED, lambda: verify_receipt(nan))


def test_rehashed_forgery_fails_semantics(buggy_receipt: ReceiptV1) -> None:
    number = buggy_receipt.counterexample_probe_no
    assert number is not None
    probes = list(buggy_receipt.probes)
    probe = probes[number - 1]
    assert probe.confirmation is not None
    probes[number - 1] = probe.model_copy(update={
        "confirmation": probe.confirmation.model_copy(update={"value": 43})})
    forged = reseal_raw(buggy_receipt, probes=[p.model_dump() for p in probes])
    raises(ReasonCode.TRUST_FAILURE, lambda: verify_receipt(forged.model_dump_json()))
    raises(ReasonCode.TRUST_FAILURE, lambda: seal(forged))


def test_seal_refuses_unsupported_clean_claim(cases: dict[str, SimulationResult]) -> None:
    receipt = cases["fixed"].receipt
    short = receipt.model_copy(update={"probes": receipt.probes[:2], "receipt_sha256": None})
    raises(ReasonCode.TRUST_FAILURE, lambda: seal(short))


def test_seal_refuses_unvalidated_receipt(buggy_receipt: ReceiptV1) -> None:
    broken = ReceiptV1.model_construct(**{**dict(buggy_receipt), "run_id": "NOT-A-UUID"})
    raises(ReasonCode.INTERNAL_ERROR, lambda: seal(broken))


# ---------------------------------------------------------------- replay


def test_counterexample_replay(buggy_receipt: ReceiptV1) -> None:
    replay = validate_replay(buggy_receipt, None)
    assert replay == buggy_receipt.replay
    assert replay.input.prices_cents == [15, 15, 15]
    assert replay.input.discount_percent == 10
    assert replay.command == build_replay_command(ExecutionMode.OFFLINE_FIXTURE,
                                                  buggy_receipt.run_id, None)
    assert validate_replay(buggy_receipt, replay.probe_no) == replay


def test_replay_for_other_probe_is_derived(cases: dict[str, SimulationResult]) -> None:
    receipt = cases["fixed"].receipt
    raises(ReasonCode.REPLAY_UNAVAILABLE, lambda: validate_replay(receipt, None))
    replay = validate_replay(receipt, 2)
    assert replay.probe_no == 2
    assert replay.input_sha256 == receipt.probes[1].candidate.input_sha256
    assert replay.command.endswith("--probe 2")
    raises(ReasonCode.REPLAY_UNAVAILABLE, lambda: validate_replay(receipt, 0))
    raises(ReasonCode.REPLAY_UNAVAILABLE,
           lambda: validate_replay(receipt, len(receipt.probes) + 1))


def test_replay_requires_intact_receipt(buggy_receipt: ReceiptV1) -> None:
    unsealed = buggy_receipt.model_copy(update={"receipt_sha256": None})
    raises(ReasonCode.RECEIPT_TAMPERED, lambda: validate_replay(unsealed, None))
    edited = buggy_receipt.model_copy(update={"seed": 99})
    raises(ReasonCode.RECEIPT_TAMPERED, lambda: validate_replay(edited, None))


@pytest.mark.parametrize("changes", [
    {"harness_sha256": "e" * 64}, {"image_digest": "sha256:" + "e" * 64},
    {"head_source_sha256": "e" * 64}, {"oracle_version": "other-oracle@1"},
    {"contract_sha256": "e" * 64},
])
def test_replay_provenance_mismatch(buggy_receipt: ReceiptV1, changes: dict[str, Any]) -> None:
    assert buggy_receipt.replay is not None
    replay = buggy_receipt.replay.model_copy(update=changes)
    forged = reseal_raw(buggy_receipt, replay=replay.model_dump())
    raises(ReasonCode.TRUST_FAILURE, lambda: validate_replay(forged, None))
    raises(ReasonCode.TRUST_FAILURE, lambda: verify_receipt(forged.model_dump_json()))


def test_replay_command_bound_to_run(buggy_receipt: ReceiptV1) -> None:
    assert buggy_receipt.replay is not None
    other = build_replay_command(ExecutionMode.OFFLINE_FIXTURE,
                                 "00000000-0000-4000-8000-000000000000", None)
    replay = buggy_receipt.replay.model_copy(update={"command": other})
    forged = reseal_raw(buggy_receipt, replay=replay.model_dump())
    raises(ReasonCode.TRUST_FAILURE, lambda: validate_replay(forged, None))


def test_build_replay_matches_receipt(cases: dict[str, SimulationResult]) -> None:
    result = cases["buggy"]
    receipt = result.receipt
    number = receipt.counterexample_probe_no
    assert number is not None
    assert receipt.pinned_revision is not None
    assert receipt.source_file_sha256 is not None
    replay = build_replay(run_id=receipt.run_id, probe=result.probes[number - 1],
                          revision=receipt.pinned_revision, sources=receipt.source_file_sha256,
                          enrollment=catalog.demo_enrollment(),
                          execution_mode=ExecutionMode.OFFLINE_FIXTURE)
    assert replay == receipt.replay
    wrong = SourceHashes(base=receipt.source_file_sha256.base, head="e" * 64)
    raises(ReasonCode.TRUST_FAILURE, lambda: build_replay(
        run_id=receipt.run_id, probe=result.probes[number - 1],
        revision=receipt.pinned_revision, sources=wrong,  # type: ignore[arg-type]
        enrollment=catalog.demo_enrollment(), execution_mode=ExecutionMode.OFFLINE_FIXTURE))
    raises(ReasonCode.TRUST_FAILURE, lambda: build_replay(
        run_id=receipt.run_id, probe=result.probes[number - 1],
        revision=receipt.pinned_revision, sources=receipt.source_file_sha256,  # type: ignore[arg-type]
        enrollment=catalog.demo_enrollment(), execution_mode=ExecutionMode.LIVE))


def test_fresh_replay_observation_binding(cases: dict[str, SimulationResult]) -> None:
    receipt = cases["buggy"].receipt
    assert receipt.replay is not None
    probe = receipt.probes[receipt.replay.probe_no - 1]
    assert probe.confirmation is not None
    assert check_replay_observation(receipt.replay, probe.head, Side.HEAD)
    assert check_replay_observation(receipt.replay, probe.base, Side.BASE)
    assert check_replay_observation(receipt.replay, probe.confirmation, Side.CONFIRM)
    assert not check_replay_observation(receipt.replay, probe.base, Side.HEAD)
    spoofed = probe.head.model_copy(update={"harness_sha256": "e" * 64})
    assert not check_replay_observation(receipt.replay, spoofed, Side.HEAD)
