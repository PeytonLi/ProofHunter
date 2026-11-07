"""CLI handler: ``research report``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from research.metrics.ledger import read_ledger, totals
from research.metrics.results import (
    ResultsError,
    read_degraded,
    read_label_files,
    read_protocol,
    read_raw_results,
)
from research.metrics.scoring import score
from research.reporting.report import render_report

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = ROOT / "spec" / "protocol-v1.json"
DEFAULT_OUT = ROOT / "artifacts" / "measurement" / "report"


def report(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="research report", description="Score raw results and render the report."
    )
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--labels", type=Path, nargs="+", required=True)
    parser.add_argument("--ledger", type=Path, nargs="*", default=[])
    parser.add_argument(
        "--degraded",
        type=Path,
        nargs="*",
        default=[],
        help='JSONL of {"run_id", "degraded": reason|null} from arms that fell back',
    )
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--unseal-test-labels",
        action="store_true",
        help="allow the sealed test-split label file (verification lane only)",
    )
    args = parser.parse_args(list(argv))
    try:
        protocol = read_protocol(args.protocol)
        rows = read_raw_results(*args.results)
        labels = [
            lab
            for f in read_label_files(args.labels, allow_sealed=args.unseal_test_labels)
            for lab in f.labels
        ]
        scores = dict(score(rows, labels, protocol, read_degraded(*args.degraded)))
        scores["ledger"] = totals(e for p in args.ledger for e in read_ledger(p))
    except (ResultsError, ValueError) as error:
        print(f"report failed: {error}", file=sys.stderr)
        return 1
    path = render_report(scores, args.out)
    print(path)
    return 0
