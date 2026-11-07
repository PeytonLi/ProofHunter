# ProofHunter research track

Versioned research and benchmarking for ProofHunter. It checks whether a learned candidate ranker
and a tuned hypothesis generator improve defect discovery on the supported checkout contract,
compares them with strong baselines, and reports latency and cost. The track is isolated from
production. It imports `proofhunter` read-only and never changes production contracts, the frozen
`bench/` benchmark or the production lock.

- Protocol (frozen v1): [`spec/RESEARCH-SPEC.md`](spec/RESEARCH-SPEC.md), machine-readable
  [`spec/protocol-v1.json`](spec/protocol-v1.json), [`spec/spend-policy.json`](spec/spend-policy.json)
- Shared row schemas: [`schemas/models.py`](schemas/models.py) (Pydantic, strict) with exported
  JSON Schemas `schemas/*.schema.json` and valid samples in `schemas/samples/`
- Cross-lane interfaces: [`schemas/interfaces.py`](schemas/interfaces.py)
- Lanes, owned paths, signatures: [`OWNERSHIP.md`](OWNERSHIP.md)

## Environment

A separate uv project. The root `pyproject.toml`/`uv.lock` stay byte-identical.

```bash
uv sync --project research --locked                 # dev group (pytest, ruff, mypy)
uv sync --project research --locked --group generator  # only inside the pinned GPU training image
```

## Commands

```bash
# setup checks (offline)
UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research
UV_OFFLINE=1 uv run --project research --locked python -m research.schemas.export --check
UV_OFFLINE=1 uv run --project research --locked mypy --strict research tests/research

# research CLI (plan section 16); each command exits 3 "not implemented" until its lane lands
uv run --project research --locked python -m research.cli --help
uv run --project research --locked python -m research.cli inspect-dataset   # dataset
uv run --project research --locked python -m research.cli prepare-splits    # dataset
uv run --project research --locked python -m research.cli run-pilot         # integration
uv run --project research --locked python -m research.cli collect-observations  # ranker
uv run --project research --locked python -m research.cli prepare-generator-examples  # generator
uv run --project research --locked python -m research.cli train-ranker      # ranker
uv run --project research --locked python -m research.cli train-generator   # generator
uv run --project research --locked python -m research.cli evaluate          # integration
uv run --project research --locked python -m research.cli report            # measurement
uv run --project research --locked python -m research.cli verify-teardown   # measurement
```

## Changing shared contracts

`schemas/`, `spec/`, `cli.py`, the research lock and these docs belong to the setup lane. Edit
`schemas/models.py`, then run `python -m research.schemas.export` to regenerate the JSON Schemas,
update the samples and bump the protocol version. Lanes rebase onto the new setup commit.

## Boundaries

- Track A (checkout contract) is the only track that executes, and only reviewed template code.
- Track B (SWE-smith) is design-only. Its schema cannot represent an executed task.
- Snap routing is disabled in controlled comparisons.
- Offline, recorded and live results are never pooled.
- Every paid call is ledgered. Teardown is verified. Single jobs over 50 USD need a coordinator check-in.
