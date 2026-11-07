from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytest.importorskip("numpy", reason="research lock only; run with --project research")

from research.cli import main as cli
from research.metrics.synthetic import label_file, make_labels, write_demo
from research.reporting.plots import bar_chart, line_chart
from research.schemas.models import Split


def test_report_cli_end_to_end_keeps_modes_separate(tmp_path: Path) -> None:
    demo = write_demo(tmp_path / "in", regressions=12, clean=4, existing=2)
    out = tmp_path / "out"
    assert (
        cli(
            [
                "report",
                "--results",
                str(demo["results"]),
                "--labels",
                str(demo["labels"]),
                "--ledger",
                str(demo["ledger"]),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    text = (out / "report.md").read_text()
    sections = [line for line in text.splitlines() if line.startswith("## ")]
    assert sections == [
        "## Offline measurements",
        "## Recorded measurements",
        "## Live measurements",
    ]
    assert "not cloud latency" in text
    assert "never pooled" in text
    assert "descriptive only" in text  # 14 defect cases < 30
    assert "unknown" in text  # storage cost unknown, not zero
    for mode in ("offline", "recorded", "live"):
        for name in (
            "recall_vs_executions",
            "recall_vs_usd",
            "recall_vs_wall_ms",
            "fp_inconclusive",
            "time_breakdown",
        ):
            ET.parse(out / f"{mode}_validation_{name}.svg")
        ET.parse(out / f"{mode}_cost_breakdown.svg")
    scores = json.loads((out / "scores.json").read_text())
    assert set(scores["modes"]) == {"offline", "recorded", "live"}
    assert {t["measurement_mode"] for t in scores["ledger"]} == {"offline", "recorded", "live"}


def test_report_refuses_sealed_labels(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    demo = write_demo(tmp_path / "in", regressions=2, clean=1, existing=0)
    sealed = tmp_path / "test.json"
    sealed.write_text(label_file(make_labels(2, 1), Split.TEST).model_dump_json())
    args = [
        "report",
        "--results",
        str(demo["results"]),
        "--labels",
        str(sealed),
        "--out",
        str(tmp_path / "o"),
    ]
    assert cli(args) == 1
    assert "sealed" in capsys.readouterr().err
    assert cli([*args, "--unseal-test-labels"]) == 0


def test_plots_are_valid_svg_and_escape_text() -> None:
    svg = line_chart("a<b", "x", "y", [("s&1", [(0.0, 0.0), (1.0, None), (2.0, 1.0)])])
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "a&lt;b" in svg
    assert "s&amp;1" in svg
    ET.fromstring(
        bar_chart("t", "y", ["c1", "c2"], [("s", [1.0, 2.0]), ("u", [0.5, 0.0])], stacked=True)
    )
    ET.fromstring(line_chart("empty", "x", "y", []))
