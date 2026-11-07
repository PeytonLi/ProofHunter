# Bayes role handoff

## Implementation and API

`proofhunter.bayes` exports `initialize(plan)`, `generate(plan, seed)`,
`select(belief, candidates, policy)`, and `update(belief, selection, observed)`
with the frozen port signatures. Pure helpers are `predict_h0`,
`predict_per_item`, `predict_half_even`, `predictions`, `likelihoods`,
`entropy`, `p_mismatch`, `posterior`, `eig`, `coverage_tags`, and
`make_candidate`; `PREDICTORS` maps the three modeled integer predictors.

Initialization keeps the first proposal per mode, restores alternatives when
needed, persists provenance, and normalizes frozen prior weights. Generation
adds the four common anchors, boundary cases, a bounded integer grid ranked by
retained constraint matches and then predictor disagreements, and version-seeded
fuzz. Deduplication is global. Selection uses finite-checked information gain,
rounded-score/hash tie breaks, and the coverage guard (number of missing tags
covered first). Update uses only a binary observation and rounds normalized mass
to twelve decimals. No production module depends on the test reference.

## Validation and errors

- `generate`: `ValueError` for bool/noninteger seeds or seeds outside the frozen domain.
- `select`: `ValueError` for empty/oversized/duplicate pools, generator version mismatch,
  unsupported belief or selection policies, or zero remaining pair budget.
- `update`: `ValueError` for a stale prior, differing likelihood/prediction keys,
  non-`BinaryOutcome` observations (including strings), or exceeding `MAX_PROBES`.
- Math helpers raise `ArithmeticError` for nonfinite calculations, nonpositive
  evidence/mass, noninterior likelihoods, negative probability mass, and negative
  EIG beyond tolerance. Tiny negative EIG is clamped to zero.
- Frozen DTO validation additionally enforces legal candidate inputs and vector shapes.

## Integration assumptions

The caller stops before `remaining_pairs` reaches zero, maintains the used-input
hash set, and calls `bank.pool_for(route.choice, used)` before selection. Pool hash
and size describe the entire supplied pool, not the coverage-filtered subset.
The caller supplies current missing coverage and remaining pair budget. Successful
paired matches alone advance clean coverage. Confirmation never calls `update`;
excluded probes never call `update`. Oracle evaluation belongs to `proofhunter.oracle`,
not the H0 predictor. No I/O, provider credentials, or provider clients are used here.

## Checks

All runtime checks used `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline`:

```text
uv run --locked pytest -q tests/bayes
18 passed in 0.36s
uv run --locked pytest -q tests/contracts
205 passed in 1.10s
uv run --locked ruff check src/proofhunter/bayes tests/bayes
All checks passed!
uv run --locked mypy src/proofhunter/bayes
Success: no issues found in 1 source file
uv run --locked python tools/gate.py setup
[gate] setup PASSED
```

Tests cover the reference vector, pinned-bank predictors/tags, golden receipt
replays, deterministic generation and plan influence, policy/budget/input errors,
coverage guard edges and tie breaks, update chains and numerical errors. Offline
end-to-end tests bind these four production functions into the setup simulator,
retaining `FixtureRunner` and the independent reference oracle/classifier.

The first offline dependency sync found uncached wheels. `uv sync --locked` then
installed the frozen environment from the package index (103 packages); no
provider calls or keys were used. Subsequent checks were offline.

## Limits

The grid is bounded and prioritizing, not an exhaustive constraint solver. The
generated bank intentionally differs from the pinned reference bank; the latter
remains the golden replay source. Hypotheses represent one dominant mode and
likelihoods are modeling assumptions, not a correctness proof. No UI is involved.
