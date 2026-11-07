# ProofHunter

ProofHunter checks whether an authorized GitHub pull request changes behavior that matters. A maintainer asks inside ASI:One, ProofHunter pins the PR to exact base and head commits, tests a small budget of informative inputs against a trusted oracle, and returns a verdict with a reproducible evidence receipt. The full specification is [ProofHunter-PRD.md](ProofHunter-PRD.md).

Status: offline integration implemented; live-capable CLI entrypoints are offline-tested.
Live provider execution and release still require operator provisioning and approval.
See [live entrypoints](docs/integration.md#live-integration-entrypoints-offline-tested-only)
for enrollment, serve/worker/stack, pinned demo, replay and retention commands.

## Quickstart (offline)

```bash
export UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline
uv sync --locked
uv run --locked pytest -q
uv run --locked python -m proofhunter demo --offline --case buggy --seed 17 --receipt ./buggy-receipt.json
uv run --locked python -m proofhunter demo --offline --case fixed --seed 17 --receipt ./fixed-receipt.json
uv run --locked python -m proofhunter replay --offline --receipt ./buggy-receipt.json --probe counterexample
uv run --locked python -m bench.run --offline --split sample --seed 17 --output ./bench-results.jsonl
uv run --locked python tools/gate.py integrated --evidence ~/ph-evidence
```

Everything defaults to `PROOFHUNTER_PROFILE=offline`. No provider credentials are read in that profile and every live feature flag is off.

`serve` requires explicitly supplied, distinct `PROOFHUNTER_TEST_API_TOKEN` and
`PROOFHUNTER_TEST_OPERATOR_TOKEN` (local test credentials only). It binds
127.0.0.1 by default. Run `uv run --locked python -m proofhunter serve --profile offline`
and `uv run --locked python -m proofhunter worker --profile offline` in separate
terminals with the same `PROOFHUNTER_DB_PATH` and effective configuration.
`/readyz` stays 503 without a fresh matching worker heartbeat (TTL 30s, refresh
10s, including active work). Durable pins and allocations precede I/O; pin-only
recovery resumes, while partial spend fails closed without refunds.
Standalone demos use a temporary database unless that path is supplied.
`half_even` and `existing` are also supported demo cases. Demo exit 0 means a
terminal evidence receipt, not necessarily a clean verdict. Live CLI requests
are refused in this offline build. See [integration notes](docs/integration.md)
for recovery limitations, golden differences and remaining release gates.

## Where things are

- `src/proofhunter/contracts.py`, `ports.py`, `config.py`: frozen shared DTOs, typed ports and settings.
- `schemas/`: JSON Schema snapshots and one sample payload per boundary.
- `fixtures/`: demo revisions, fixture adapters for every port, provider wire examples, golden receipts.
- `tools/gate.py`: `setup` (G0) and `integrated` (G2) gates.
- `docs/setup.md`: import paths, fixtures, ownership and mode boundaries. `docs/provider-smoke.md`: live smoke steps for the operator.
