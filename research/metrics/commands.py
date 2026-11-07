"""CLI handler: ``research verify-teardown``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from research.metrics.ledger import open_ledger, read_ledger
from research.metrics.teardown import verify_teardown as audit
from research.schemas.models import LedgerEntry, SpendPolicy

DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "spec" / "spend-policy.json"


class _Combined:
    def __init__(self, paths: Sequence[Path]) -> None:
        self._entries = [e for p in paths if p.exists() for e in read_ledger(p)]

    def append(self, entry: LedgerEntry) -> None:
        raise RuntimeError("audit ledgers are read-only")

    def entries(self) -> Sequence[LedgerEntry]:
        return self._entries


def verify_teardown(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="research verify-teardown", description="Audit ledgers for unverified teardown."
    )
    parser.add_argument("--ledger", type=Path, nargs="+", required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(list(argv))
    policy = SpendPolicy.model_validate_json(args.policy.read_text(encoding="utf-8"))
    missing = [str(p) for p in args.ledger if not p.exists()]
    if missing:
        print(f"missing ledger files: {missing}", file=sys.stderr)
        return 2
    ledger = open_ledger(args.ledger[0]) if len(args.ledger) == 1 else _Combined(args.ledger)
    problems = audit(policy, ledger)
    print(
        json.dumps(
            {
                "entries": len(ledger.entries()),
                "resources": len(
                    {
                        (e.provider, e.resource_id)
                        for e in ledger.entries()
                        if e.resource_id is not None
                    }
                ),
                "unverified": [
                    {
                        "entry_id": e.entry_id,
                        "provider": e.provider,
                        "resource_id": e.resource_id,
                        "paid": e.paid,
                    }
                    for e in problems
                ],
                "ok": not problems,
            },
            indent=2,
        )
    )
    return 0 if not problems else 1
