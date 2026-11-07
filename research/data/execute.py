"""Run reviewed Track A template output in-process (never dataset or target-repository code)."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Sequence
from typing import Final

from proofhunter import bayes
from proofhunter import contracts as c
from proofhunter.oracle.rounding import rounding_half_up_once

Input = tuple[tuple[int, ...], int]
Entry = Callable[[list[int], int], object]
PROBE_SET_VERSION: Final = "probe-set-v1"


def load_entry(source: bytes) -> Entry:
    """Execute one reviewed template rendering in a fresh namespace and return ``total_cents``."""
    namespace: dict[str, object] = {"__name__": "checkout"}
    exec(compile(source, "checkout.py", "exec"), namespace)  # noqa: S102 - reviewed template output
    entry = namespace["total_cents"]
    if not callable(entry):
        raise TypeError("total_cents missing")
    return entry  # type: ignore[return-value]


def call(entry: Entry, item: Input) -> int | None:
    """Return the entry result, or ``None`` for an exception or an invalid output."""
    try:
        value = entry(list(item[0]), item[1])
    except Exception:  # noqa: BLE001 - any failure is an unusable outcome
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def expected(item: Input) -> int:
    return rounding_half_up_once(list(item[0]), item[1])


def builtin_plan() -> c.HypothesisPlan:
    return c.HypothesisPlan(
        hypotheses=c.builtin_hypothesis_payload().hypotheses,
        provenance=c.ProviderProvenance(provider=c.ProviderName.META,
                                        mode=c.ProviderMode.FALLBACK, model_id=None,
                                        request_id=None, latency_ms=None, attempts=0,
                                        reason_code=None))


def pool(seed: int) -> list[c.Candidate]:
    return list(bayes.generate(builtin_plan(), seed).candidates)


def pool_seed(candidate_seed: int, replicate_seed: int) -> int:
    """Dataset-lane convention for combining the case seed with a replicate seed."""
    digest = hashlib.sha256(f"pool-seed-v1:{candidate_seed}:{replicate_seed}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def probe_set(replicate_seeds: Sequence[int], master_seed: int) -> list[Input]:
    """Deterministic in-domain inputs used to validate, fingerprint and pick witnesses."""
    out: dict[Input, None] = {}

    def add(prices: Sequence[int], discount: int) -> None:
        out.setdefault((tuple(prices), discount), None)

    for seed in replicate_seeds:
        for cand in pool(seed):
            add(cand.prices_cents, cand.discount_percent)
    discounts = (0, 1, 5, 10, 15, 20, 25, 33, 50, 67, 75, 90, 99, 100)
    for n in range(0, 9):
        for d in discounts:
            for pattern in ((15,), (99,), (1,), (10000,), (5, 15, 25), (50, 150, 1),
                            (333, 1000, 2500, 7, 9999, 0, 45, 12)):
                add([pattern[k % len(pattern)] for k in range(n)], d)
            add(list(range(1, n + 1)), d)
            add([1000 * (k + 1) for k in range(n)], d)
    rng = random.Random(f"{PROBE_SET_VERSION}:{master_seed}")
    while len(out) < 3200:
        n = rng.randint(0, 8)
        mode = rng.random()
        if mode < 0.4:
            prices = [rng.randint(0, 200) for _ in range(n)]
        elif mode < 0.7:
            prices = [rng.choice((5, 15, 25, 35, 45, 55, 65, 75, 85, 95)) for _ in range(n)]
        else:
            prices = [rng.randint(0, 10000) for _ in range(n)]
        add(prices, rng.randint(0, 100))
    return list(out)
