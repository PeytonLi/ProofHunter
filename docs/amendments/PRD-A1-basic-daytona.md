# Amendment PRD-A1: basic-tier Daytona for the live sandbox

Amendment ID: `PRD-A1-basic-daytona@2026-11-07`
Approved by: Peyton (repository owner), 2026-11-07
Status: active. This amendment supersedes the strict "block-all outbound egress"
requirement in ProofHunter-PRD.md sections 3 and 7 for the enrolled demo scope
below. Every other PRD requirement still applies.

## Decision

Peyton approved running live verifications on the **basic (Tier 1) Daytona
account** on 2026-11-07, verbatim:

> "Accept the risk and finish the demo on basic Daytona (demo repo only, no
> secrets, sandbox deleted after each run)"

Earlier instruction, still binding:

> "just use the basic Daytona account for now. Do not upgrade the Daytona
> account. Do not change its org-wide network policy, because the account is
> shared with ForkSafe."

## Findings that motivated this amendment

The benign canary on the basic account (docs/canary/2026-11-07-daytona-basic-tier.md)
measured:

- **Egress is restricted, not blocked.** DNS fails entirely; HTTP on port 80 is
  denied by the provider filter (`Internet is restricted on Tier 1 and Tier 2`);
  but TCP/443 by IP address (GitHub, PyPI) still completes TLS and returns 200.
  The PRD's strict block-all contract cannot be met on this tier.
- **Residual risk, explicitly accepted:** the Daytona host gateway
  `172.20.0.1:22` answers with an SSH banner from inside the sandbox. This is
  provider infrastructure, not a ProofHunter endpoint. Peyton accepted this
  exposure as residual risk. It must never be logged into or probed further.
- Cloud metadata (169.254.169.254, 169.254.170.2) is not reachable: port 80 is
  intercepted by the provider restriction filter and HTTPS is reset.
- No ProofHunter or Render endpoint is reachable by hostname, and no credentials
  exist in the sandbox environment or common credential paths.
- Resource limits hold (1 CPU, 1 GiB); the image stays digest-pinned; strict
  timeouts fire; deleted sandboxes are gone within seconds.

## Compensating controls (enforced in code, tested)

Each control is enforced in `src/proofhunter/` and `ops/sandbox/`, not just
documented here:

1. **Enrollment allowlist.** The live runner accepts exactly
   `PeytonLi/ProofHunter-Demo` (case-sensitive). Any other repository is refused
   at startup and at pipeline resolution (`runner.approval.allowed_repository`).
2. **Zero injection.** Sandboxes are created with `env_vars={}` and
   `secrets={}`. No Daytona API key, GitHub credential, Agentverse key, model
   key, or any other secret or runtime environment is passed in.
3. **Pinned image.** The runner verifies at every attestation/observation that
   the configured snapshot exists, is active, and is bound to
   `python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54`.
   A mutable alias or a mismatch refuses the run.
4. **Ephemeral sandboxes with verified deletion.** Every sandbox is created
   `ephemeral=True`, deleted after its run, and the control plane is polled until
   the sandbox is absent (bounded at 30 s). Failure to observe absence is a
   recorded infrastructure failure (`deletion_unverified` in the append-only
   ledger) and blocks further runs.
5. **Strict timeouts.** Control-plane and execution deadlines remain enforced;
   the wrapper kills the process group on cleanup.
6. **Empty network namespace for the untrusted child.** Because Tier 1 egress is
   only restricted, the untrusted child additionally runs in a fresh, empty
   network namespace (`os.unshare(CLONE_NEWNET)` as root, before
   `setgroups([])`, `setgid(65534)`, `setuid(65534)`, no-new-privileges). Inside
   that namespace the child cannot reach the provider gateway, public IPs, or
   metadata at all; the canary verifies this on every run.
7. **Trusted harness only.** Only `wrapper.py` and `canary.py` (root-written to
   `/opt/proofhunter/` inside the sandbox) plus the bounded source/request
   payload are uploaded. The harness SHA-256 is enrolled in the approval
   fingerprint.
8. **Approval file.** `runner-isolation-approval.json` must carry
   `"amendment": "PRD-A1-basic-daytona@2026-11-07"`, the actual recorded canary
   output in the amended schema (see below), the config fingerprint, and the
   image digest. It expires and is refreshed like the original approval.

## Amended canary schema

The tier-3 canary (`egress_denied`) is replaced by the restricted-egress schema
in `runner.approval.CANARY_EXPECTED`: `egress_policy: "restricted"`,
`dns_denied`, `metadata_denied`, `secrets_absent`, `restricted_child`,
`child_network_isolated`, and a fixed `probes_denied` map. Metadata probes count
as denied unless a real metadata service answers 2xx; the provider's 403
restriction filter counts as denied. Child probes verify the empty network
namespace blocks the gateway, public IPs and metadata.

## Boundaries

- This amendment covers only `PeytonLi/ProofHunter-Demo` live runs.
- It does not authorize upgrading the Daytona account or changing org-wide
  network policy.
- It does not weaken metadata, DNS, credential-absence, timeout, resource-limit,
  deletion-verification or receipt requirements.
- Revocation condition: any observed credential, metadata 2xx, internal
  ProofHunter/Render endpoint, or control failure invalidates this approval and
  requires a new explicit decision.
