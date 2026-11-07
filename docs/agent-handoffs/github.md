VERDICT: PASS

# GitHub lane handoff

## Live staging update (in-memory PEM factory and real demo PR pins)

Branch `devin/1794083400-proofhunter-live/live-github`, from exact base
`4140bf0dfab8d090181b6aea37cbb7e574eb75a6`. Only GitHub-owned paths changed.

What changed:

- `_session` now loads the App key through `proofhunter.config.github_private_key_pem`,
  so `GITHUB_PRIVATE_KEY` (in memory) and the `GITHUB_PRIVATE_KEY_PATH` fallback both
  work. Errors never echo key or path. Load sites should use
  `load_settings(role=ServiceRole.WORKER)`; agent and API roles get no App ID or key, so
  the factory refuses them. Disabled checks never build a session or read the key.
- `GithubHTTP` takes an optional `observer`. Each call yields a `CallRecord` with method,
  a route template (installation ID, PR number and SHAs replaced by placeholders), HTTP
  status, latency in ms, sanitized `x-github-request-id` and UTC time. No headers, bodies
  or credentials are kept. `create_source_port(..., observer=...)` is keyword-only and
  optional, so the frozen signature still works.
- `python -m proofhunter.github.smoke` is the operator smoke. It needs
  `PROOFHUNTER_PROFILE=live`, worker-role settings and checks/webhooks off. It uses
  the production factory, takes 1 to 4 `--case PR_URL BASE_SHA HEAD_SHA` cases, resolves
  each through `GithubRepositoryValidator.resolve`, and stops on a SHA mismatch. Limits:
  1 token call plus 7 calls per case, and 120 s total. The public report has no operator
  IDs, token, key or source text. `--private-out` gets repo ID, installation ID and the
  enrollment fingerprint.

Live result, 2026-11-07 20:40:59Z to 20:41:02Z UTC, API version 2026-03-10: PASS. There
were 15 calls in a budget of 15, all 200/201, with latencies of 143 to 390 ms. Config
fingerprint `0b0129f0352c17f1da59266e8bd15bd9ebdcd0b2c338feb3801b972656fb01b4`. Token
permissions were exactly contents, pull_requests and metadata read, with no checks.

| PR | Pinned base | Pinned head | base blob / sha256 | head blob / sha256 |
|---|---|---|---|---|
| #1 buggy | aa8cfe2b3ec9f438363bc7de3e6aed6b4fce5db4 | d9d6443c5b3266cc8087b5fb30e5fe5c5198fdf6 | 8f0b603c / 07de083d...968f (387 B) | c9b75248 / 69918784...96b8 (283 B) |
| #2 fixed | b2c8b83c9c150e6d85aeb5771290a95bb5410fcd | 9e4ed8d954edc6573cc3e592cadb64c5becb4efb | f903e7aa / 67ad174f...42ff (229 B) | 8f0b603c / 07de083d...968f (387 B) |

Both PRs are merged, and GitHub still returned the original base and head SHAs. Every
`checkout.py` was a `100644 blob` and matched its tree blob ID and recomputed Git blob
SHA-1. As an independent check, a plain `git fetch` of the public demo repo at those
four commits gave the same blob IDs, SHA-256 values and sizes. No code was executed and
the demo repo was not modified.

Known gaps:

- Setup/config: the saved `PROOFHUNTER_GITHUB_APP_PRIVATE_KEY` is one line, with
  newlines collapsed to spaces. `config._validated_pem` rejects it. For this smoke the
  operator rebuilt the newlines in process memory before binding `GITHUB_PRIVATE_KEY`.
  Either re-save the secret with real or `\n` newlines, or have setup accept
  whitespace-separated single-line PEM.
- Setup/config: worker role also requires `MODEL_API_KEY` and `SUBQ_API_KEY`, so a
  GitHub-only smoke has to load them even though it never uses them. A scoped GitHub
  role or per-provider check would be tighter.
- Integration: the staging enrollment for `PeytonLi/ProofHunter-Demo` lives only on the
  operator host, not in git. Its snapshot and image digest fields are still the offline
  example values until the runner gate passes.
- Checks and webhooks stay off. No checks permission was requested.

Commands (offline, `UV_OFFLINE=1 PROOFHUNTER_PROFILE=offline`): `uv sync --locked`,
`pytest -q tests/github` (135 passed), full `pytest -q` (908 passed), `ruff check .`,
`mypy src bench tools tests/integration`, `tools/gate.py setup` and `integrated`
(PASSED). No pre-commit config exists.

## Original offline handoff

Branch: `devin/1794078900-proofhunter/github`

Starting G0 SHA: `5c3aecd0ec98eb7ad1a91c43d85bf940d52d67cf` (not current main).
The exact resulting commit is returned in the final handoff report.

## Implemented boundaries

All four required factories are exported from `proofhunter.github`:

- `create_source_port(settings, enrollments)` returns `GithubSource` for live wiring.
- `create_check_port(settings)` returns `GithubChecks`; offline/disabled calls do no I/O.
- `create_repository_validator(settings, enrollments)` returns the strict enrollment validator.
- `create_webhook_verifier(settings)` returns the feature-gated HMAC/event helper.

The HTTP-backed classes accept injected `GithubSession`/`GithubHTTP` clients for realistic
mock testing. Factory clients use the fixed GitHub API origin, no environment proxy,
no redirects, pinned API version, bounded JSON bytes and a 15-second total request deadline.
Errors expose only typed reason codes, retryability and fixed sanitized messages.
Call `await source.aclose()` / `await checks.aclose()` at application shutdown.

### Repository and source

Canonical URLs are parsed with `urlsplit` plus exact path/authority validation. Userinfo,
ports, escapes, Unicode, whitespace, fragments, query delimiters, ambiguous paths,
nonpositive/overflow PR numbers and unenrolled/disabled repositories are rejected.
Case-normalized names and immutable repository/installation IDs must match enrollment.

App authentication signs an RS256 JWT with a ten-minute interval. Installation tokens
remain opaque, memory-only and cached by installation/repository until sixty seconds
before expiry. Requests specify one `repository_ids` entry and exact minimum permissions;
the response must confirm that exact public repository, permissions and UTC expiry.
Source credentials have no checks permission. Check credentials request `checks:write`
only for an explicitly enabled live check adapter.

`pin()` validates both public repository identities and pins exact lowercase base/head
SHAs. Fork/deleted-repository/missing-ref responses are rejected. `fetch()` never reads
moving branch refs: it follows each pinned Git commit to its tree, verifies one root
`checkout.py` entry with type `blob` and mode `100644`, then checks contents against the
tree blob ID and recomputed Git blob SHA-1. Source SHA-256 and byte counts are recorded.
Directories, symlinks, executable modes, submodules, LFS pointers, invalid base64,
non-UTF-8/NUL content, oversize/truncated responses and source/tree drift fail closed.
No clone, download URL, hook, target import or target execution is used.

Model context uses a local diff, a bounded head excerpt and the PRD contract description.
Known credential patterns or unexpected controls withhold both diff and excerpt; they
do not modify the verified execution source. Total context remains below 32 KiB.

### Advisory checks

Offline intents and disabled flags skip all HTTP. Enabled publication finds exactly
one associated open PR and re-fetches it to verify public same-repository identity and
the current head. Missing/multiple open associations or changed heads cannot publish.
Checks are completed advisory results, bound to exact `head_sha` and run UUID
`external_id`, with fixed public-safe output and the receipt hash. Shared
`check_conclusion_for()` owns verdict-to-conclusion mapping.

Before any create, list all app checks for the commit and match app/name/head/external
ID. Reconcile existing checks (PATCH incomplete results), hold duplicate matches and
incomplete/paginated listings, and serialize concurrent calls. A lost/malformed create
acknowledgement triggers reconciliation, never another blind POST. If no definitive
match exists, return `held_ambiguous`; the same adapter instance never re-POSTs that run.

### Webhooks

Verify exact raw-body HMAC-SHA256 with `compare_digest` before JSON parsing. Limit the
body to 256 KiB; reject malformed signatures, duplicate JSON keys, excessive nesting,
invalid IDs, booleans as IDs, malformed SHAs, private/fork/mismatched repositories and
inconsistent PR numbers. Only `opened`, `reopened`, `synchronize` are relevant.
Each successful `verify()` authorizes one `parse()` of those exact bytes on the same
instance; duplicate deliveries must authenticate again. The returned DTO includes
the raw-body SHA-256 for durable fingerprint dedupe. No DB or enqueue side effects.

## Frozen interface assumptions for adjacent owners

1. Offline integration binds `fixtures.adapters.FixtureSource` / `FixtureChecks`, as
   setup specifies. Calling the live source factory with offline settings raises
   `ConfigError`, rather than silently using fixtures or reading a private key.
   Repository validation remains usable offline; check/webhook factories fail closed.
2. `SourcePort.fetch()` has no run-ID parameter. Its context carries the same explicit
   placeholder UUID used by the setup fixture. Integration must rebind `context.run_id`
   to the actual run before calling Meta. The contract description is the PRD's
   reviewed checkout rule; oracle ownership still verifies contract bytes/fingerprint.
3. `CheckIntent` has no PR number. The adapter conservatively resolves a unique open
   commit-associated PR, then re-fetches its exact head. Multi-PR ambiguity refuses
   publication. This is a safe restriction, not a claim of general multi-PR support.
4. Store/integration must persist the outbox dispatch attempt *before* external I/O,
   save returned check IDs/attempts, and retain `held_ambiguous` for operator review.
   A process crash or canceled dispatch is ambiguous: never reset it to a fresh
   create. An in-memory adapter tombstone is not a durable outbox. The frozen port
   has no persisted attempt argument or reconcile-only method, so interrupted
   dispatches must remain held after restart until operator reconciliation.
5. The webhook route owns the enabled/404 gate, raw-body read limit and response
   status mapping. Call `verify(raw, signature)` and check its boolean before `parse`.
   Resolve the parsed canonical URL through `RepositoryValidator`, compare returned
   enrollment repository/installation IDs with event IDs, re-pin the PR, reject
   mismatched event heads as `stale_event`, and atomically dedupe delivery ID plus
   body fingerprint with enqueue. Authentication alone does not authorize a repo.
6. The store creates live-only intents from immutable sealed receipts, using the
   shared conclusion mapper; it never forwards user-supplied check names/output.
   The adapter cannot make GitHub head recheck and create atomic, but every check
   remains bound to the original immutable SHA, never a claim about a later head.

## Verification commands and observed results

Commands ran in this isolated worktree. Verification commands used
`PROOFHUNTER_PROFILE=offline UV_OFFLINE=1`.

| Command | Result |
| --- | --- |
| `git fetch origin` | PASS |
| `git checkout -b devin/1794078900-proofhunter/github 5c3aecd0ec98eb7ad1a91c43d85bf940d52d67cf` | Exact G0 SHA confirmed |
| `PROOFHUNTER_PROFILE=offline uv sync --locked` | PASS; filled this VM's pinned dependency cache |
| `uv run --locked pytest -q tests/github --tb=short` | 115 passed |
| `uv run --locked ruff check src/proofhunter/github tests/github` | PASS |
| `uv run --locked mypy src/proofhunter/github tests/github` | PASS; 7 files, strict mode |
| `uv run --locked python tools/gate.py setup` | PASS; 205 contract tests, lock checks, setup lint/types, 107 fixture/schema files unchanged |
| `git diff --cached --check` | PASS |

No configured pre-commit hook/config exists (only Git sample hooks). Normal commit
hook execution is not bypassed. Scope is limited to this handoff, the GitHub package
and GitHub tests; frozen shared contracts, fixtures, schemas and `uv.lock` are unchanged.

HTTP fixtures use reviewed `fixtures.catalog` metadata/source bytes and the recorded
GitHub pull response. Commit/tree/content/token/check/webhook responses are constructed
with realistic wire shapes and explicit mutations. Adjacent SourcePort/CheckPort DTOs
and frozen contract fixtures are used directly. No Meta, Snap, runner, oracle, store
or control API implementation is imported. Tests deny real socket connects/DNS;
an ephemeral in-memory RSA key tests actual App JWT signing without live credentials.

## Defects, blockers and remaining gates

Setup-owner defects: none. Offline lane blockers: none.

Live permission/expiry/API-version smoke, installation access, real throwaway-repo
check replay/staleness, webhook delivery/replay, and durable outbox/route integration
remain separate owner/operator gates. No live provider was contacted, no live key
was read, no target source was executed, and no check/webhook was enabled externally.
No deploy, agent publication, merge or PR creation was performed. Only the assigned
working branch is to be pushed for the coordinator's code handoff.
