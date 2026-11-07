VERDICT: PASS

# GitHub lane handoff

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
