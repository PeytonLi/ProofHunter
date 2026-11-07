"""Gate result model shared by every verification audit."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    severity: Severity = Severity.ERROR
    subject: str | None = None


@dataclass
class GateResult:
    gate: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)

    def error(self, code: str, message: str, subject: str | None = None) -> None:
        self.findings.append(Finding(code, message, Severity.ERROR, subject))

    def warn(self, code: str, message: str, subject: str | None = None) -> None:
        self.findings.append(Finding(code, message, Severity.WARNING, subject))

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "passed": self.passed, "stats": self.stats,
                "findings": [asdict(f) for f in self.findings]}


def render_markdown(results: Iterable[GateResult], title: str = "Verification report") -> str:
    rows = list(results)
    lines = [f"# {title}", "", "| Gate | Result | Errors | Warnings |", "|---|---|---|---|"]
    for r in rows:
        errs = sum(f.severity is Severity.ERROR for f in r.findings)
        warns = len(r.findings) - errs
        lines.append(f"| {r.gate} | {'PASS' if r.passed else 'FAIL'} | {errs} | {warns} |")
    for r in rows:
        if not r.findings and not r.stats:
            continue
        lines += ["", f"## {r.gate}", ""]
        if r.stats:
            lines.append("```json")
            lines.append(json.dumps(r.stats, indent=2, sort_keys=True, default=str))
            lines.append("```")
        for f in r.findings[:200]:
            subj = f" ({f.subject})" if f.subject else ""
            lines.append(f"- {f.severity.value.upper()} {f.code}{subj}: {f.message}")
        if len(r.findings) > 200:
            lines.append(f"- ... {len(r.findings) - 200} more findings omitted")
    return "\n".join(lines) + "\n"
