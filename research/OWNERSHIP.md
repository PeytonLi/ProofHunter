# Research lane ownership (research-protocol-v1)

Setup commit base: `b97210ac37e9a352f7fcd5a3c4f013ab876b39a6` (contains verified candidate
`8dec16a0f7c8f5c0773a02c396f84c0bbf2de378`). Every lane branches from the setup commit on
`devin/1794090000-research/setup` and edits **only** its owned paths. Anything not listed is
read-only for every lane.

## Global rules

- Production is read-only: `src/`, `bench/` (including `bench/policy.json`), `schemas/`,
  `fixtures/`, `tools/`, `config/`, root `pyproject.toml`, root `uv.lock`, `.github/`.
  `tests/research/test_setup_protocol.py` fails if the root locks or `bench/policy.json` change.
- Shared contracts are owned by **setup** and frozen: `research/schemas/**`, `research/spec/**`,
  `research/cli.py`, `research/pyproject.toml`, `research/uv.lock`, `research/README.md`,
  `research/OWNERSHIP.md`, `research/__init__.py`, `tests/research/test_setup_*.py`,
  `research/.gitignore`. Need a change? Ask the coordinator; setup publishes a new
  protocol commit and every lane rebases. Do not edit these in a lane branch.
- Dependencies: a lane that needs a new package asks setup to add it to `research/pyproject.toml`
  and re-lock. Do not edit the lock in a lane branch.
- Rows crossing lane boundaries are the frozen Pydantic models in `research/schemas/models.py`
  (JSON Schemas beside them). Validate on write and on read. Extra fields are rejected.
- Behavioural interfaces are the `typing.Protocol`s in `research/schemas/interfaces.py`.
  Implement them structurally; do not subclass production contracts.
- CLI: `research/cli.py` dispatches `command -> <module>:<function>(argv: Sequence[str]) -> int`.
  The owning lane creates that module/function (table below); `cli.py` is never edited. A missing
  module keeps the stub behaviour (exit 3, "not implemented").
- Each Python package directory a lane creates needs an `__init__.py`. Tests live in the lane's
  `tests/research/<lane>/` directory (pytest uses `--import-mode=importlib`) and must pass offline:
  `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline uv run --project research --locked pytest -q tests/research`.
- Production EIG, oracle and candidate generators are imported read-only
  (`proofhunter.bayes`, `proofhunter.oracle.rounding`, `proofhunter.contracts`). Never copy or
  fork them.
- No secrets in files, rows, logs or commits. Every paid call appends a `LedgerEntry`
  (see `research/spec/spend-policy.json`).
- Artifacts (generated datasets, checkpoints, raw rows, ledgers) go under `research/artifacts/<lane>/`
  (git-ignored by convention; commit only small frozen manifests and hashes the spec asks for).

## Lanes

| Lane | Owned paths | CLI commands |
| --- | --- | --- |
| dataset | `research/data/**`, `tests/research/dataset/**`, `research/artifacts/dataset/` | `inspect-dataset` -> `research.data.commands:inspect_dataset`; `prepare-splits` -> `research.data.commands:prepare_splits` |
| ranker | `research/ranker/**`, `tests/research/ranker/**`, `research/artifacts/ranker/` | `collect-observations` -> `research.ranker.commands:collect_observations`; `train-ranker` -> `research.ranker.commands:train_ranker` |
| generator | `research/generator/**`, `research/training/**`, `tests/research/generator/**`, `research/artifacts/generator/` | `prepare-generator-examples` -> `research.generator.commands:prepare_generator_examples`; `train-generator` -> `research.training.commands:train_generator` |
| baselines | `research/baselines/**`, `tests/research/baselines/**`, `research/artifacts/baselines/` | none (arms are consumed by `evaluate`) |
| measurement | `research/metrics/**`, `research/reporting/**`, `tests/research/metrics/**`, `tests/research/reporting/**`, `research/artifacts/measurement/` | `report` -> `research.reporting.commands:report`; `verify-teardown` -> `research.metrics.commands:verify_teardown` |
| integration | `research/execution/**`, `tests/research/execution/**`, `research/artifacts/execution/` | `run-pilot` -> `research.execution.commands:run_pilot`; `evaluate` -> `research.execution.commands:evaluate` |
| verification | `research/verification/**`, `tests/research/verification/**`, `research/artifacts/verification/` | none; read-only on every other lane |

## Interfaces each lane must provide

Types come from `research.schemas.models` (`M`), `research.schemas.interfaces` (`I`) and
`proofhunter.contracts` (`C`). Signatures are frozen; internals are the lane's choice.

### dataset (`research/data/`)

```python
# research/data/templates.py   reviewed, deterministic, parameterized checkout variants
TEMPLATE_REGISTRY_VERSION: str
def list_templates() -> Sequence[TemplateInfo]  # template_id, template_version, template_sha256, family_id, kind
def render(template_id: str, params: Mapping[str, ParamValue]) -> tuple[bytes, bytes]  # (base, head) checkout.py
# research/data/build.py
def build_cases(protocol: M.ProtocolV1, out_dir: Path) -> M.CaseManifest  # writes manifest + LabelFiles
# research/data/splits.py
def assign_splits(cases: Sequence[M.CaseRecord], protocol: M.ProtocolV1) -> M.CaseManifest
# research/data/store.py
def open_store(root: Path) -> I.CaseStore  # labels("test") raises unless unseal == protocol unseal token
# research/data/swe_smith.py   Track B, metadata only; must never import or execute dataset code
def prepare_swe_smith_manifest(source: Path, out: Path) -> Sequence[M.SweSmithTask]
# research/data/commands.py
def inspect_dataset(argv: Sequence[str]) -> int
def prepare_splits(argv: Sequence[str]) -> int
```

Emits: `CaseManifest`, `LabelFile` per split (test sealed), `SweSmithTask` rows. Only reviewed
template code runs (to compute witnesses with the trusted oracle). No overlap with
`bench/corpus` or `bench/holdout` source hashes; check with `bench.holdout` read-only.

### ranker (`research/ranker/`)

```python
# research/ranker/features.py
FEATURE_SET_VERSION: str
class CheckoutFeatures(I.FeatureExtractor): ...  # pre-execution features only (no forbidden prefixes)
# research/ranker/collect.py
def collect_observations(store: I.CaseStore, split: M.Split, seeds: Sequence[int],
                         policy: str) -> Iterator[M.RankingObservation]  # train/validation only
# research/ranker/train.py
class LambdaRankTrainer(I.RankerTrainer): ...  # LightGBM lambdarank; groups = decision points
# research/ranker/selector.py
def load_ranker_selector(checkpoint: Path, *, arm_id: str) -> I.Selector
# research/ranker/commands.py
def collect_observations(argv: Sequence[str]) -> int
def train_ranker(argv: Sequence[str]) -> int
```

Unknown-relevance and untested candidates are excluded from training targets, never zero.
Checkpoint selection uses validation only.

### generator (`research/generator/`, `research/training/`)

```python
# research/generator/prompt.py
PROMPT_FORMAT_VERSION: str
def build_input(sources: I.CaseSources) -> M.GeneratorInput  # no labels/template params/fix patch
def render_prompt(prompt: M.GeneratorInput) -> str
def parse_plan(text: str) -> C.HypothesisPlanPayload  # validates against production contract; else raises
# research/generator/examples.py
def build_examples(store: I.CaseStore, split: M.Split) -> Iterator[M.GeneratorExample]
# research/generator/model.py
def load_generator(kind: Literal["qwen_untuned", "qwen_qlora"], *,
                   adapter: Path | None, revision: str) -> I.HypothesisGenerator
# research/training/qlora.py
class QloraTrainer(I.GeneratorTrainer): ...  # Fireworks LoRA SFT if adapter export works, else Runpod
# research/generator/commands.py, research/training/commands.py
def prepare_generator_examples(argv: Sequence[str]) -> int
def train_generator(argv: Sequence[str]) -> int
```

Freezes base model revision, tokenizer revision, prompt format, adapter sha256.

### baselines (`research/baselines/`)

```python
# research/baselines/selectors.py   fixed_order, uniform_random, simple_heuristic, builtin_eig
def make_selector(arm_id: str, *, seed: int) -> I.Selector
# research/baselines/meta.py
def meta_generator() -> I.HypothesisGenerator  # production MetaPort (muse-spark), Snap disabled
# research/baselines/frontier.py
def frontier_reviewer(model_id: str) -> I.Selector  # no execution feedback
def frontier_agent(model_id: str, *, free_input: bool = False) -> I.Selector
# research/baselines/models.json   frozen exact provider model IDs + verification date
```

`builtin_eig` wraps `proofhunter.bayes.select/update` read-only. Frontier model IDs are verified
against the provider listing before freezing; no invented IDs.

### measurement (`research/metrics/`, `research/reporting/`)

```python
# research/metrics/ledger.py
def open_ledger(path: Path) -> I.Ledger  # append-only JSONL of M.LedgerEntry
# research/metrics/scoring.py
def score(rows: Iterable[M.RawResultRow], labels: Iterable[M.CaseLabel],
          protocol: M.ProtocolV1) -> Mapping[str, object]  # per arm x mode x budget
def bootstrap_ci(values: Mapping[str, Sequence[float]], *, resamples: int, seed: int
                 ) -> tuple[float, float, float]  # keyed by case_id (or family_id)
def paired_difference(a: Iterable[M.RawResultRow], b: Iterable[M.RawResultRow],
                      protocol: M.ProtocolV1) -> Mapping[str, object]
# research/metrics/teardown.py
def verify_teardown(policy: M.SpendPolicy, ledger: I.Ledger) -> Sequence[M.LedgerEntry]
# research/reporting/report.py
def render_report(scores: Mapping[str, object], out_dir: Path) -> Path
# research/metrics/commands.py, research/reporting/commands.py
def verify_teardown(argv: Sequence[str]) -> int
def report(argv: Sequence[str]) -> int
```

Offline, recorded and live results are never pooled. Misses are censored.

### integration (`research/execution/`)

```python
# research/execution/pool.py
def shared_pool(case: M.CaseRecord, protocol: M.ProtocolV1) -> C.CandidateBank  # same for every arm
# research/execution/runner.py
def make_runner(protocol: M.ProtocolV1, ledger: I.Ledger) -> I.EpisodeRunner
# research/execution/commands.py
def run_pilot(argv: Sequence[str]) -> int
def evaluate(argv: Sequence[str]) -> int  # frozen checkpoints, all arms, preregistered seeds
```

Owns the episode loop: coverage guard, six paired probes, one fresh-head confirmation, bounded
retries, trusted oracle, `RawResultRow` emission. Executes only reviewed template sources.

### verification (`research/verification/`)

Independent re-run of frozen artifacts: schema validation of every row, split leakage audit,
hash checks against manifests and the spec, recomputation of headline metrics from raw rows,
teardown audit from ledgers. Writes `research/artifacts/verification/` only.

## Track B

Track B (`SweSmithTask`) is data preparation only. No lane may execute SWE-smith repository or
test code anywhere until the spec's unblock conditions are met and setup issues a new protocol
version.
