# ProofHunter

ProofHunter verifies whether a GitHub pull request on an enrolled repository changes behavior that matters. It pins the PR to immutable base and head commits, proposes falsifiable bug hypotheses, spends a small test budget on the most informative inputs and returns a verdict with a reproducible evidence receipt.

Keywords: GitHub, pull request, PR verification, regression, counterexample, evidence receipt, rounding bug, Agents That Prove Their Work.

## Commands

- `verify https://github.com/OWNER/REPO/pull/N` starts a run and returns its run ID.
- `status <run_id>` shows phase and attempts while active, and the verdict when complete.
- `evidence <run_id>` shows the minimal counterexample (expected, base, head, repeated head), pinned SHAs and replay command, or coverage and limits for other outcomes.
- `help` explains the supported scope.

## Scope

Public, same-repository Python PRs on repositories enrolled by the operator (demo contract `checkout-rounding@1`). Forks, private repositories, user-supplied commands, models, oracles or remotes are refused.
