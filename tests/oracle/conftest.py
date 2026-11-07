from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fixtures import catalog
from fixtures.simulate import SimulationResult, simulate_case
from proofhunter.contracts import Enrollment, ReceiptV1

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / "fixtures" / "demo" / "receipts"


@pytest.fixture
def enrollment() -> Enrollment:
    return catalog.demo_enrollment()


@pytest.fixture
def golden_bytes() -> Callable[[str], bytes]:
    def load(name: str) -> bytes:
        return (RECEIPTS / f"{name}.seed17.json").read_bytes()
    return load


@pytest.fixture(scope="session")
def cases() -> dict[str, SimulationResult]:
    return {name: simulate_case(name) for name in catalog.PR_CASES}


@pytest.fixture
def buggy_receipt(cases: dict[str, SimulationResult]) -> ReceiptV1:
    return cases["buggy"].receipt
