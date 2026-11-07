"""Small synthetic Track A case store following the frozen schemas (development and tests).

The templates below are reviewed in this file; they are the only code the fixture executes.
Test-split labels are never needed: ``labels`` refuses every split, so any read fails loudly.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Final

from proofhunter.contracts import canonical_sha256
from research.schemas.interfaces import CaseSources
from research.schemas.models import CaseLabel, CaseManifest, CaseRecord, ParamValue, Split, Track

BASE: Final = """def total_cents(prices_cents, discount_percent):
    raw = sum(prices_cents) * (100 - discount_percent)
    whole, rest = divmod(raw, 100)
    return whole + (1 if rest >= 50 else 0)
"""

_HALF_UP: Final = """
def _half_up(raw):
    whole, rest = divmod(raw, 100)
    return whole + (1 if rest >= 50 else 0)
"""

# family -> (head body template, parameter grid); {k} is the family parameter.
HEADS: Final[dict[str, tuple[str, tuple[int, ...]]]] = {
    "clean": ("""def total_cents(prices_cents, discount_percent):
    raw = sum(prices_cents) * (100 - discount_percent)  # variant {k}
    return _half_up(raw)
""", (1, 2, 3, 4)),
    "per_item": ("""def total_cents(prices_cents, discount_percent):
    if len(prices_cents) < {k}:
        return _half_up(sum(prices_cents) * (100 - discount_percent))
    return sum(_half_up(p * (100 - discount_percent)) for p in prices_cents)
""", (1, 2, 3, 4)),
    "half_even": ("""def total_cents(prices_cents, discount_percent):
    raw = sum(prices_cents) * (100 - discount_percent)
    whole, rest = divmod(raw, 100)
    if rest == 50 and sum(prices_cents) >= {k}:
        return whole + (whole % 2)
    return _half_up(raw)
""", (0, 10, 100, 1000)),
    "floor": ("""def total_cents(prices_cents, discount_percent):
    raw = sum(prices_cents) * (100 - discount_percent)
    if discount_percent >= {k}:
        return raw // 100
    return _half_up(raw)
""", (1, 25, 50, 75)),
    "ceil": ("""def total_cents(prices_cents, discount_percent):
    raw = sum(prices_cents) * (100 - discount_percent)
    if sum(prices_cents) >= {k}:
        return -(-raw // 100)
    return _half_up(raw)
""", (0, 500, 5000, 50000)),
    "large_total": ("""def total_cents(prices_cents, discount_percent):
    raw = sum(prices_cents) * (100 - discount_percent)
    if sum(prices_cents) > {k}:
        return raw // 100
    return _half_up(raw)
""", (1000, 10000, 100000, 1000000)),
}
SPLITS: Final[dict[str, Split]] = {
    "per_item": Split.TRAIN, "floor": Split.TRAIN, "clean": Split.TRAIN,
    "half_even": Split.VALIDATION, "ceil": Split.VALIDATION,
    "large_total": Split.TEST,
}


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def render(family: str, k: int) -> tuple[bytes, bytes]:
    body, _ = HEADS[family]
    return BASE.encode(), (body.format(k=k) + _HALF_UP).encode()


class FixtureStore:
    """Implements ``CaseStore`` over the reviewed fixture templates."""

    def __init__(self) -> None:
        cases: list[CaseRecord] = []
        self._sources: dict[str, tuple[bytes, bytes]] = {}
        for family, (body, grid) in HEADS.items():
            for i, k in enumerate(grid):
                base, head = render(family, k)
                case_id = f"fx-{family}-{i:02d}"
                params: dict[str, ParamValue] = {"k": k}
                cases.append(CaseRecord(
                    schema_version="research-case-v1", track=Track.A, case_id=case_id,
                    split=SPLITS[family], family_id=family, defect_origin=f"fx-{family}@1",
                    near_dup_cluster=f"fx-{family}", template_id=f"fx-{family}",
                    template_version="1", template_sha256=_sha(body), params=params,
                    params_sha256=canonical_sha256(params), contract_id="checkout-rounding@1",
                    base_source_sha256=_sha(base), head_source_sha256=_sha(head),
                    candidate_seed=int(_sha(case_id)[:8], 16)))
                self._sources[case_id] = (base, head)
        self._manifest = CaseManifest(
            schema_version="research-case-manifest-v1", track=Track.A,
            manifest_version="ranker-fixture-v1", split_policy="grouped-family-origin-cluster-v1",
            dataset_commit="0" * 40, cases=cases,
            cases_sha256=canonical_sha256([x.model_dump(mode="json") for x in cases]))
        self.label_reads: list[str] = []

    def manifest(self) -> CaseManifest:
        return self._manifest

    def sources(self, case_id: str) -> CaseSources:
        case = next(x for x in self._manifest.cases if x.case_id == case_id)
        base, head = self._sources[case_id]
        return CaseSources(case=case, base_source=base, head_source=head)

    def labels(self, split: str, *, unseal: str | None = None) -> Sequence[CaseLabel]:
        self.label_reads.append(split)
        raise PermissionError("the ranker never reads labels")


def verify_fixture(sources: CaseSources) -> None:
    """Reviewer for fixture cases: bytes must equal a re-render of the reviewed template."""
    family = sources.case.family_id
    k = sources.case.params["k"]
    if not isinstance(k, int) or render(family, k) != (sources.base_source, sources.head_source):
        from research.ranker.executor import UnreviewedSourceError
        raise UnreviewedSourceError(sources.case.case_id)
