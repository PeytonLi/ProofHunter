# ProofHunter

ProofHunter checks whether an authorized GitHub pull request changes behavior that matters. A maintainer asks inside ASI:One, ProofHunter pins the PR to exact base and head commits, tests a small budget of informative inputs against a trusted oracle, and returns a verdict with a reproducible evidence receipt. The full specification is [ProofHunter-PRD.md](ProofHunter-PRD.md).

Status: G0 setup freeze. Shared contracts, ports, fixtures and gates are in place; role modules land after G1.

## Quickstart (offline)

```bash
uv sync --locked
UV_OFFLINE=1 python tools/gate.py setup
uv run --locked pytest -q tests/contracts
```

Everything defaults to `PROOFHUNTER_PROFILE=offline`. No provider credentials are read in that profile and every live feature flag is off.

## Where things are

- `src/proofhunter/contracts.py`, `ports.py`, `config.py`: frozen shared DTOs, typed ports and settings.
- `schemas/`: JSON Schema snapshots and one sample payload per boundary.
- `fixtures/`: demo revisions, fixture adapters for every port, provider wire examples, golden receipts.
- `tools/gate.py`: `setup` (G0) and `integrated` (G2) gates.
- `docs/setup.md`: import paths, fixtures, ownership and mode boundaries. `docs/provider-smoke.md`: live smoke steps for the operator.
