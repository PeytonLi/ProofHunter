"""Independent verification audits for the ProofHunter research track (owner: verification).

Every audit is a pure function over recorded artifacts that returns a ``GateResult``.
No audit executes dataset or target-repository code.
"""

from research.verification.gates import Finding, GateResult, Severity

__all__ = ["Finding", "GateResult", "Severity"]
