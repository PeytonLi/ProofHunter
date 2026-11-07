"""Trusted oracle, evidence comparison, verdict rules, receipts and replay (owner: oracle).

Pure synchronous functions bound by ``proofhunter.ports``: ``evaluate``
(``Evaluator``), ``classify`` (``Classifier``), ``compute_coverage``
(``CoverageFn``), ``seal`` (``Sealer``), ``verify_receipt`` (``ReceiptVerifier``)
and ``validate_replay`` (``ReplayValidator``).
"""

from proofhunter.oracle.evidence import (
    PairAssessment,
    ProvenanceBinding,
    assess_probe,
    classify_pair,
    confirmation_status,
    exact_value,
    provenance_ok,
    trusted_expected,
)
from proofhunter.oracle.receipts import (
    MAX_RECEIPT_BYTES,
    build_replay,
    check_receipt_semantics,
    check_replay_observation,
    replay_binding,
    seal,
    validate_replay,
    verify_receipt,
)
from proofhunter.oracle.rounding import (
    TRUSTED_ORACLES,
    evaluate,
    legal_input,
    rounding_half_up_once,
)
from proofhunter.oracle.verdict import (
    BLOCKING_STOP_REASONS,
    EvidenceInconsistency,
    audit_probes,
    classify,
    compute_coverage,
)

__all__ = [
    "BLOCKING_STOP_REASONS",
    "MAX_RECEIPT_BYTES",
    "TRUSTED_ORACLES",
    "EvidenceInconsistency",
    "PairAssessment",
    "ProvenanceBinding",
    "assess_probe",
    "audit_probes",
    "build_replay",
    "check_receipt_semantics",
    "check_replay_observation",
    "classify",
    "classify_pair",
    "compute_coverage",
    "confirmation_status",
    "evaluate",
    "exact_value",
    "legal_input",
    "provenance_ok",
    "replay_binding",
    "rounding_half_up_once",
    "seal",
    "trusted_expected",
    "validate_replay",
    "verify_receipt",
]
