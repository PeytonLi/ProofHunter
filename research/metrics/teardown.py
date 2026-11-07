"""Teardown audit over the ledger: every provisioned resource needs a verified teardown."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from research.schemas import interfaces as I
from research.schemas.models import LedgerEntry, SpendPolicy


def verify_teardown(policy: SpendPolicy, ledger: I.Ledger) -> Sequence[LedgerEntry]:
    """Return offending entries: the latest entry of each resource never verified torn down,
    and paid entries from providers the spend policy does not guard."""
    guarded = {p.provider for p in policy.providers}
    resources: dict[tuple[str, str], list[LedgerEntry]] = defaultdict(list)
    problems: list[LedgerEntry] = []
    for entry in ledger.entries():
        if entry.paid and entry.provider not in guarded:
            problems.append(entry)
        if entry.resource_id is not None:
            resources[(entry.provider, entry.resource_id)].append(entry)
    for items in resources.values():
        if not any(e.teardown_verified is True for e in items):
            latest = max(items, key=lambda e: e.recorded_at)
            if latest not in problems:
                problems.append(latest)
    return sorted(problems, key=lambda e: (e.recorded_at, e.entry_id))
