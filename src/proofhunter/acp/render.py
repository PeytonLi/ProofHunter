"""Redacted chat reports (PRD section 1 chat behavior, section 7 claims).

Everything rendered comes from validated public DTOs (``RunStatus``, ``ReceiptV1``)
or fixed strings in this module. Control API error messages, provider bodies,
tokens and operator identifiers are never echoed. Any fixture execution carries an
explicit offline label.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Final

from proofhunter.acp.commands import InvalidReason
from proofhunter.contracts import (
    ACTIVE_STATES,
    VERDICT_WORDING,
    ExecutionMode,
    PublicObservation,
    PublicProbeRecord,
    ReasonCode,
    ReceiptV1,
    RunState,
    RunStatus,
    Verdict,
)

#: Hard cap on one chat reply (characters).
MAX_REPLY_CHARS: Final = 3500
OFFLINE_LABEL: Final = ("Mode: offline_fixture. This run used reviewed fixture execution, "
                        "not live GitHub, Meta, Snap or Daytona.")
LIVE_LABEL: Final = "Mode: live."

HELP_TEXT: Final = (
    "ProofHunter checks whether a GitHub pull request on an enrolled repository changes "
    "behavior that matters, and returns a verdict with a reproducible evidence receipt.\n"
    "Supported scope: public, same-repository Python PRs on repositories the operator "
    "enrolled (demo contract checkout-rounding@1). Forks, private repos and other "
    "repositories are refused.\n"
    "Commands:\n"
    "  verify https://github.com/OWNER/REPO/pull/N  start a run, returns a run ID\n"
    "  status <run_id>  phase and attempts while active, verdict when complete\n"
    "  evidence <run_id>  counterexample or coverage, receipt hash and replay command\n"
    "  help  this message\n"
    "Verdicts: confirmed_regression (reproduced on head only), confirmed_existing_defect "
    "(reproduced on base and head), no_defect_found (in the reported scope and budget), "
    "inconclusive (with the reason and what is missing)."
)

_INVALID_TEXT: Final[Mapping[InvalidReason, str]] = {
    InvalidReason.EMPTY: "I did not find a command.",
    InvalidReason.TOO_LONG: "That message is too long for a command.",
    InvalidReason.CONTROL_CHARACTERS: "That message contains control characters.",
    InvalidReason.NOT_ASCII_URL: "PR URLs must be plain ASCII.",
    InvalidReason.INVALID_PR_URL: (
        "I only accept a canonical PR URL of the form https://github.com/OWNER/REPO/pull/N "
        "(no query, fragment, credentials or extra path)."),
    InvalidReason.MULTIPLE_URLS: "Send exactly one PR URL per request.",
    InvalidReason.MISSING_URL: "Add the PR URL: verify https://github.com/OWNER/REPO/pull/N",
    InvalidReason.MISSING_RUN_ID: "Add the run ID, for example: status <run_id>",
    InvalidReason.INVALID_RUN_ID: "Run IDs are full UUIDs as returned by verify.",
    InvalidReason.AMBIGUOUS: "That request is ambiguous. Send one command at a time.",
    InvalidReason.UNKNOWN: "I did not recognize that command.",
}

_REASON_TEXT: Final[Mapping[ReasonCode, str]] = {
    ReasonCode.INVALID_PR_URL: "The PR URL is not a canonical GitHub pull request URL.",
    ReasonCode.REPO_NOT_ENROLLED: (
        "That repository is not enrolled. ProofHunter only verifies repositories an operator "
        "enrolled with a reviewed contract."),
    ReasonCode.FORK_UNSUPPORTED: "Pull requests from forks are outside the supported scope.",
    ReasonCode.FORBIDDEN: "That request is outside the supported scope.",
    ReasonCode.REVISION_UNAVAILABLE: "The pinned base or head revision could not be read.",
    ReasonCode.ORACLE_UNAVAILABLE: "No trusted oracle was available for this contract.",
    ReasonCode.ISOLATION_UNVERIFIED: "Sandbox isolation could not be verified.",
    ReasonCode.PROVIDER_UNAVAILABLE: "A required service was unavailable.",
    ReasonCode.PROVIDER_TIMEOUT: "A required service timed out.",
    ReasonCode.BUDGET_EXHAUSTED: "The test budget ran out.",
    ReasonCode.NONDETERMINISTIC: "The head result did not reproduce on a fresh run.",
    ReasonCode.COVERAGE_INSUFFICIENT: "Required coverage was not reached.",
    ReasonCode.STALE_EVENT: "The PR head moved; this result applies only to the pinned SHA.",
    ReasonCode.CONFLICTING_IDEMPOTENCY_KEY: "This message ID was already used for another request.",
    ReasonCode.RATE_LIMITED: "Too many runs right now.",
    ReasonCode.NOT_FOUND: "No run with that ID is visible.",
    ReasonCode.RECEIPT_PENDING: "The run is still active, so there is no receipt yet.",
    ReasonCode.FEATURE_DISABLED: "New verification runs are paused by the operator.",
    ReasonCode.UNAUTHORIZED: "ProofHunter is misconfigured (service authentication failed).",
    ReasonCode.INVALID_REQUEST: "The request was rejected as invalid.",
    ReasonCode.CONFIRMED_EVIDENCE: "Confirmed with a fresh head replay.",
    ReasonCode.COVERAGE_COMPLETE: "Required coverage completed.",
    ReasonCode.INPUTS_EXHAUSTED: "All candidate inputs were used.",
    ReasonCode.DEADLINE_EXCEEDED: "The run deadline passed.",
    ReasonCode.TRUST_FAILURE: "A trust check failed, so no verdict is claimed.",
    ReasonCode.PREEXISTING_MISMATCH_CHANGED: "A pre-existing mismatch changed value on head.",
    ReasonCode.UNSUPPORTED_CONTRACT: "The contract is not supported.",
    ReasonCode.SOURCE_INVALID: "The enrolled source file was missing or invalid.",
    ReasonCode.INTERNAL_ERROR: "ProofHunter hit an internal error.",
}


def safe_text(value: object, limit: int = 200) -> str:
    """Single-line, control-free, length-capped text."""
    text = "".join(" " if unicodedata.category(ch)[0] in {"C", "Z"} and ch != " " else ch
                   for ch in str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def cap_reply(text: str) -> str:
    cleaned = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) != "Cc")
    if len(cleaned) <= MAX_REPLY_CHARS:
        return cleaned
    return cleaned[: MAX_REPLY_CHARS - 16].rstrip() + "\n[truncated]"


def explain_reason(code: ReasonCode | None) -> str:
    if code is None:
        return "No reason recorded."
    return _REASON_TEXT.get(code, "Reason code recorded.") + f" ({code.value})"


def render_help() -> str:
    return HELP_TEXT


def render_invalid(reason: InvalidReason) -> str:
    return f"{_INVALID_TEXT[reason]}\n\n{HELP_TEXT}"


def render_error(code: ReasonCode, *, retry_after_seconds: int | None = None,
                 retryable: bool = False) -> str:
    lines = [explain_reason(code)]
    if code in {ReasonCode.REPO_NOT_ENROLLED, ReasonCode.FORK_UNSUPPORTED,
                ReasonCode.FORBIDDEN, ReasonCode.INVALID_PR_URL}:
        lines.append("Type help to see the supported scope.")
    if retry_after_seconds is not None:
        lines.append(f"Try again in about {retry_after_seconds} seconds.")
    elif retryable:
        lines.append("Please try again shortly; resending the same request is safe.")
    return "\n".join(lines)


def _short(sha: str | None) -> str:
    return sha[:7] if sha else "unknown"


def _mode_label(mode: ExecutionMode | None) -> str:
    if mode is ExecutionMode.LIVE:
        return LIVE_LABEL
    if mode is ExecutionMode.OFFLINE_FIXTURE:
        return OFFLINE_LABEL
    return "Mode: pending. Execution details are not available yet; check status."


def render_queued(status: RunStatus) -> str:
    detail = status.detail
    lines = [f"Run accepted. Run ID: {status.run_id}", f"State: {status.status.value}"]
    if detail is not None and detail.repo_full_name and detail.pr_number:
        lines.append(f"PR: {safe_text(detail.repo_full_name)} #{detail.pr_number}")
    lines.append(_mode_label(detail.execution_mode if detail else None))
    lines.append(f"Check progress with: status {status.run_id}")
    if status.status in ACTIVE_STATES:
        lines.append("The run continues in the background after this reply.")
    return "\n".join(lines)


def render_status(status: RunStatus, *, execution_mode: ExecutionMode | None = None) -> str:
    detail = status.detail
    limits = status.budget.limits
    used = status.budget.used
    lines = [f"Run {status.run_id}", f"State: {status.status.value}"]
    if detail is not None and detail.repo_full_name and detail.pr_number:
        lines.append(f"PR: {safe_text(detail.repo_full_name)} #{detail.pr_number}, "
                     f"base {_short(detail.base_sha)}, head {_short(detail.head_sha)}")
    if status.status in ACTIVE_STATES:
        lines.append(f"Attempts spent: {status.attempts_used} of "
                     f"{limits.max_sandbox_invocations} sandbox invocations, "
                     f"{used.pairs} of {limits.max_pairs} probe pairs")
        if detail is not None and detail.coverage is not None:
            cov = detail.coverage
            lines.append(f"Coverage: {len(cov.observed)} of {len(cov.required)} required tags")
        lines.append(_mode_label(detail.execution_mode if detail else None))
        lines.append(f"Poll again with: status {status.run_id}")
        return "\n".join(lines)
    if status.status is RunState.FAILED:
        lines.append("The job failed before producing a verdict.")
        lines.append(f"Reason: {explain_reason(status.reason_code)}")
    else:
        verdict = status.verdict
        assert verdict is not None
        lines.append(f"Verdict: {verdict.value}")
        if verdict is not Verdict.INCONCLUSIVE:
            lines.append(VERDICT_WORDING[verdict])
        lines.append(f"Reason: {explain_reason(status.reason_code)}")
    if status.is_stale:
        lines.append("The PR head has moved since this run; the result applies only to the "
                     "pinned head SHA.")
    if detail is not None:
        ev = detail.evidence
        lines.append(f"Evidence: {ev.matches} matches, {ev.head_mismatches} head mismatches, "
                     f"{ev.shared_mismatches} shared mismatches, {ev.base_mismatches} base "
                     f"mismatches, {ev.excluded} excluded, {ev.confirmations} confirmations")
        if detail.provider_modes:
            modes = ", ".join(f"{name.value} {mode.value}"
                              for name, mode in sorted(detail.provider_modes.items()))
            lines.append(f"Providers: {modes}")
    lines.append(f"Attempts spent: {status.attempts_used}")
    lines.append(_mode_label(detail.execution_mode if detail else execution_mode))
    if status.receipt_sha256:
        lines.append(f"Receipt SHA-256: {status.receipt_sha256}")
    lines.append(f"Details with: evidence {status.run_id}")
    return "\n".join(lines)


def _value(observation: PublicObservation | None) -> str:
    if observation is None:
        return "not run"
    if observation.value is None:
        return f"unusable ({observation.outcome.value})"
    return f"{observation.value}"


def _counterexample(receipt: ReceiptV1) -> PublicProbeRecord | None:
    number = receipt.counterexample_probe_no
    return next((p for p in receipt.probes if p.probe_no == number), None)


def render_evidence(receipt: ReceiptV1) -> str:
    verdict = receipt.verdict
    lines = [f"**Verdict: {verdict.value if verdict else 'none (job failed)'}**", ""]
    if verdict is not None:
        lines.extend([VERDICT_WORDING[verdict], ""])
    lines.append(f"- {_mode_label(receipt.execution_mode)}")
    lines.append(f"- Reason: {explain_reason(receipt.reason_code)}")
    pinned = receipt.pinned_revision
    if pinned is not None:
        lines.extend(["", f"PR: {safe_text(pinned.owner)}/{safe_text(pinned.repo)} "
                      f"#{pinned.number}"])
    probe = _counterexample(receipt)
    if probe is not None and receipt.replay is not None:
        candidate = probe.candidate
        lines.extend(["", "**Minimal reproducible input**", "", "```python",
                      f"prices_cents = {list(candidate.prices_cents)}",
                      f"discount_percent = {candidate.discount_percent}", "```", "",
                      "**Results (cents)**", ""])
        expected = probe.oracle.expected_cents
        lines.append(f"- Expected: {expected if expected is not None else 'unavailable'}")
        lines.append(f"- Base: {_value(probe.base)}")
        lines.append(f"- Head: {_value(probe.head)}")
        lines.append(f"- Repeated head: {_value(probe.confirmation)}")
        lines.extend(["", "**Replay**", "", "```bash", receipt.replay.command, "```"])
    else:
        lines.extend(["", "**Coverage and limits**", ""])
        cov = receipt.coverage
        observed = ", ".join(t.value for t in cov.observed) or "none"
        missing = ", ".join(t.value for t in cov.missing) or "none"
        lines.append(f"- Coverage: {len(cov.observed)} of {len(cov.required)} required tags, "
                     f"{cov.distinct_usable_pairs} distinct usable pairs")
        lines.append(f"- Observed: {observed}")
        lines.append(f"- Missing: {missing}")
        lines.append(f"- Probes completed: {len(receipt.probes)}")
        limits = receipt.budget.limits
        lines.append(f"- Limits: {limits.max_pairs} probe pairs, {limits.max_sandbox_invocations} "
                     f"sandbox invocations, {limits.deadline_seconds} s deadline. "
                     "Results apply only to the enrolled contract and the pinned SHAs.")
    lines.extend(["", "**Receipt details**", "", f"Run ID: `{receipt.run_id}`", ""])
    if pinned is not None:
        lines.extend([f"Base SHA: `{pinned.base_sha}`", "",
                      f"Head SHA: `{pinned.head_sha}`", ""])
    lines.append(f"- Oracle version: {safe_text(receipt.policy_versions.oracle_version, 64)}")
    if probe is not None:
        lines.append(f"- Oracle trust: {probe.oracle.trust_tier.value}")
        lines.append(f"- Route: {probe.route.choice.value} ({probe.route.mode.value}), "
                     f"EIG {probe.eig_bits} bits")
    lines.extend(["", "Receipt SHA-256:", "", f"`{receipt.receipt_sha256}`"])
    return "\n".join(lines)


__all__ = [
    "HELP_TEXT",
    "MAX_REPLY_CHARS",
    "OFFLINE_LABEL",
    "cap_reply",
    "explain_reason",
    "render_error",
    "render_evidence",
    "render_help",
    "render_invalid",
    "render_queued",
    "render_status",
    "safe_text",
]
