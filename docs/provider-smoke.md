# Live CLI staging prerequisites

The live-capable CLI is covered with offline fakes only. No provider smoke was
performed by the integration-entrypoint change. Follow `docs/integration.md` for
commands. Enroll real public GitHub identities to an external mode-0600 registry;
never use `config/enrollments.example.json` for live startup. Keep checks and
webhooks off. Until independent runner provisioning is approved, enroll with
`--unprovisioned-runner`, keep live runner off, and expect honest live
`inconclusive / isolation_unverified` receipts with no target upload.

# Provider smoke steps

ProofHunter defaults to offline mode. Nothing in this file runs in automation. An authorized operator runs these steps from a separate staging host with production credentials, then records sanitized evidence in the table at the end. Fallbacks count as failed provider gates in live smoke.

Pinned versions (from `uv.lock`): uagents 0.26.1, uagents-core 0.4.12, daytona 0.231.0 (stable v0 client), httpx 0.28.1, pydantic 2.14.3, fastapi 0.162.2, pyjwt 2.15.1. Documentation status is as of November 7, 2026.

## Common rules

- Use `PROOFHUNTER_PROFILE=live` only on the staging host. Never paste keys into the repo, issues or chat.
- Record only: sanitized request ID, UTC timestamp, HTTP status, latency, and the configuration fingerprint (`sha256` of the non-secret settings: model IDs, API version, snapshot ID, image digest).
- Never record response bodies, prompts, tokens or headers.

## Meta Model API (hypotheses)

1. Set `MODEL_API_KEY` and `META_MODEL=muse-spark-1.3`.
2. Send one request shaped like `fixtures/providers/meta_request.json` to `POST https://api.meta.ai/v1/chat/completions` with `response_format.type=json_schema`, named strict schema `hypothesis_plan` (the `HypothesisPlanPayload` schema in `schemas/HypothesisPlanPayload.schema.json`).
3. Pass: `finish_reason=stop`, `refusal` is null, inner JSON validates locally as `HypothesisPlanPayload`, no tool calls. Confirm the current token limit parameter name (`max_tokens` in the fixture) and the supported schema subset.
4. Record entitlement, latency and data-handling approval.

## SubQ Snap 1.1 (routing)

1. Set `SUBQ_API_KEY` and `SUBQ_MODEL=subq-snap-1.1`.
2. `GET https://api.subq.ai/v1/models` must list `subq-snap-1.1` (shape: `fixtures/providers/snap_models.json`).
3. Send `fixtures/providers/snap_request.json` to `POST https://api.subq.ai/v1/systemone`. Pass: `answers.next_action.type == "choice"`, `choice` is one of the offered keys, optional probabilities finite and normalized within 0.01. Compare with `fixtures/providers/snap_response.json` and record any shape difference as a contract defect.
4. Show that the chosen strategy changed the candidate batch (`CandidateBank.pool_for`). Record account limits.

## GitHub App (source pin and checks)

1. Set `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY_PATH`, `GITHUB_API_VERSION=2026-03-10`. Permissions: Contents read, Pull requests read, Metadata read. Add Checks write only when check publishing is being tested.
2. Exchange the App JWT for an installation token (`POST /app/installations/{id}/access_tokens`); treat it as opaque.
3. `GET /repos/{owner}/{repo}/pulls/{number}` on the public demo PR (shape: `fixtures/providers/github_pull.json`); pin base/head SHAs and repository IDs.
4. Fetch `checkout.py` via contents at each SHA (`github_contents_head.json`) and verify mode `100644`, type `blob` and blob SHA against the commit tree (`github_tree_head.json`).
5. Publish the exact demo PR URLs and commit IDs in the repo (replace the offline fixture values in `config/` on the staging host only) before live verification.
6. Checks and webhooks stay off (`PROOFHUNTER_ENABLE_GITHUB_CHECKS=false`, `PROOFHUNTER_ENABLE_GITHUB_WEBHOOK=false`) until their P1 gates pass on a throwaway repo.

## Daytona (isolated execution)

1. Set `DAYTONA_API_KEY`, `DAYTONA_API_URL` if required, `DAYTONA_SNAPSHOT`, `DAYTONA_IMAGE_DIGEST`. Keep `PROOFHUNTER_ENABLE_LIVE_RUNNER=false` until every step passes.
2. Create a sandbox with `CreateSandboxFromSnapshotParams(snapshot=..., public=False, network_block_all=True)` and run the runner owner's benign canary from `ops/sandbox/` before uploading any PR code.
3. Pass: denied DNS and HTTPS to a controlled destination, denied link-local and cloud-metadata access, effective policy inspected, upload works, CPU/memory limits and the 5-second timeout behave, processes are cleaned up and the sandbox is deleted.
4. If the account tier or canary fails, live untrusted execution stays disabled; use the labeled offline fixture.

## Fetch.ai Agentverse and ASI:One (ACP)

1. Set `AGENT_SEED_PHRASE` (stable) on the ACP host only. The pinned uagents 0.26.1 uses `Agent(name=..., seed=AGENT_SEED_PHRASE, port=AGENT_PORT, mailbox=True)`; there is no mailbox key variable in this version. Start `python -m proofhunter.acp`, open the printed inspector link and connect the mailbox from the Agentverse account. Verify this against the pinned SDK before release.
2. Publish the description, keywords, examples and the chat protocol manifest (`publish_manifest=True`).
3. In a fresh ASI:One conversation: find the agent by a descriptive query, invoke it by address, poll `status <run_id>`, request `evidence <run_id>`.

## Evidence log

| Provider | UTC timestamp | Request ID (sanitized) | Status | Latency ms | Config fingerprint | Result | Operator |
|---|---|---|---|---|---|---|---|
| Meta | pending | | | | | | |
| Snap models | pending | | | | | | |
| Snap decision | pending | | | | | | |
| GitHub pin and fetch | pending | | | | | | |
| Daytona canary | pending | | | | | | |
| Agentverse and ASI:One | pending | | | | | | |
