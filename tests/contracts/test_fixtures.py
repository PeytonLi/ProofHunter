"""Fixture adapters satisfy the frozen ports and the PRD reference behavior."""

import asyncio
import json
from pathlib import Path

import pytest

from fixtures import adapters, catalog, reference
from fixtures.simulate import simulate_case
from proofhunter import contracts as c
from proofhunter import ports

REPO = Path(__file__).resolve().parents[2]
SENTINEL = ([15, 15, 15], 10)
ORDER = c.HYPOTHESIS_ORDER


def test_fixture_adapters_implement_ports() -> None:
    store = adapters.InMemoryRunStore()
    assert isinstance(adapters.FixtureSource(), ports.SourcePort)
    assert isinstance(adapters.FixtureMeta(), ports.MetaPort)
    assert isinstance(adapters.FixtureRoute(), ports.RoutePort)
    assert isinstance(adapters.FixtureRunner(), ports.RunnerPort)
    assert isinstance(adapters.FixtureChecks(), ports.CheckPort)
    assert isinstance(store, ports.RunStore)
    assert isinstance(store, ports.ControlStore)
    assert isinstance(adapters.FixtureControlApi(store), ports.ControlApiClient)
    assert isinstance(adapters.FixtureRepositoryValidator(), ports.RepositoryValidator)
    assert isinstance(adapters.FixtureWebhookVerifier(), ports.WebhookVerifier)
    assert isinstance(adapters.SteppingClock(), ports.Clock)
    assert isinstance(adapters.SequentialIds(), ports.IdFactory)


@pytest.mark.parametrize(("label", "expected"), [("base", 41), ("fixed", 41), ("buggy", 42),
                                                 ("half_even", 40)])
def test_demo_revisions_reference_values(label: str, expected: int) -> None:
    observation = asyncio.run(_observe(label, *SENTINEL))
    assert observation.value == expected
    assert reference.oracle_expected(*SENTINEL) == 41


async def _observe(label: str, prices: list[int], discount: int) -> c.SandboxObservation:
    enrollment = catalog.demo_enrollment()
    revision = catalog.revisions()[label]
    data = c.CandidateInput(prices_cents=prices, discount_percent=discount)
    request = c.ExecutionRequest(
        run_id=adapters.SequentialIds().new_run_id(),
        attempt_id=adapters.SequentialIds().new_attempt_id(), side=c.Side.HEAD,
        commit_sha=revision.commit_sha, source_path="checkout.py",
        source_content=revision.source_bytes.decode(), source_sha256=revision.source_sha256,
        input=data, input_sha256=c.input_sha256(data), entrypoint="checkout.total_cents",
        resources=c.ResourcePolicy(), snapshot_id=enrollment.snapshot_id,
        image_digest=enrollment.image_digest, harness_version=enrollment.harness_version,
        harness_sha256=enrollment.harness_sha256)
    return await adapters.FixtureRunner(enrollment).observe(request)


def test_runner_denies_unknown_source_bytes() -> None:
    enrollment = catalog.demo_enrollment()
    data = c.CandidateInput(prices_cents=[1], discount_percent=0)
    source = "def total_cents(p, d):\n    return 0\n"
    request = c.ExecutionRequest(
        run_id=adapters.SequentialIds().new_run_id(),
        attempt_id=adapters.SequentialIds().new_attempt_id(), side=c.Side.HEAD,
        commit_sha="a" * 40, source_path="checkout.py", source_content=source,
        source_sha256=c.sha256_hex(source.encode()), input=data,
        input_sha256=c.input_sha256(data), entrypoint="checkout.total_cents",
        resources=c.ResourcePolicy(), snapshot_id=enrollment.snapshot_id,
        image_digest=enrollment.image_digest, harness_version=enrollment.harness_version,
        harness_sha256=enrollment.harness_sha256)
    observation = asyncio.run(adapters.FixtureRunner(enrollment).observe(request))
    assert observation.outcome is c.ObservationOutcome.DENIED
    assert observation.value is None


def test_reference_vector() -> None:
    prior = dict(zip(ORDER, (0.6, 0.15, 0.15, 0.1), strict=True))
    q = reference.likelihoods(prior, *SENTINEL)
    assert [q[h] for h in ORDER] == [0.02, 0.98, 0.98, 0.20]
    assert reference.p_mismatch(prior, q) == pytest.approx(0.326, abs=1e-12)
    post = reference.posterior(prior, q, c.BinaryOutcome.MISMATCH)
    assert [round(post[h], 5) for h in ORDER] == [0.03681, 0.45092, 0.45092, 0.06135]
    assert sum(post.values()) == pytest.approx(1.0)


def test_pinned_bank_sentinel_unique_highest_eig() -> None:
    bank = c.CandidateBank.model_validate_json(
        (catalog.DEMO_DIR / "candidate_bank.seed17.json").read_bytes())
    assert bank == reference.build_bank(17)
    assert len(bank.candidates) <= 64
    belief = reference.initialize(c.HypothesisPlan(
        hypotheses=adapters.load_plan_payload().hypotheses,
        provenance=c.ProviderProvenance(provider=c.ProviderName.META,
                                        mode=c.ProviderMode.OFFLINE_FIXTURE, model_id=None,
                                        request_id=None, latency_ms=0, attempts=0,
                                        reason_code=None))).belief
    scores = sorted(((round(reference.score(belief, k), 12), k.prices_cents, k.discount_percent)
                     for k in bank.candidates), reverse=True)
    assert (scores[0][1], scores[0][2]) == SENTINEL
    assert scores[0][0] > scores[1][0]
    anchors = {(tuple(k.prices_cents), k.discount_percent) for k in bank.candidates
               if k.is_anchor}
    assert anchors == set(c.COMMON_ANCHORS)
    sentinel = next(k for k in bank.candidates if k.prices_cents == SENTINEL[0])
    assert {c.CoverageTag.HALF_CENT, c.CoverageTag.MULTI_ITEM} <= set(sentinel.coverage_tags)
    for strategy in c.Strategy:
        assert bank.pool_for(strategy, ())


def test_prior_policy_v1_renormalizes() -> None:
    plan = c.HypothesisPlan(hypotheses=[], provenance=c.ProviderProvenance(
        provider=c.ProviderName.META, mode=c.ProviderMode.FALLBACK, model_id=None,
        request_id=None, latency_ms=None, attempts=1, reason_code=c.ReasonCode.PROVIDER_TIMEOUT))
    initial = reference.initialize(plan)
    assert initial.hypotheses.plan_source is c.PlanSource.BUILTIN_FALLBACK
    assert [initial.belief.probabilities[h] for h in ORDER] == [0.6, 0.15, 0.15, 0.1]


@pytest.mark.parametrize(("case", "verdict", "chain"), [
    ("buggy", c.Verdict.CONFIRMED_REGRESSION, (41, 41, 42, 42)),
    ("half_even", c.Verdict.CONFIRMED_REGRESSION, (41, 41, 40, 40)),
    ("existing", c.Verdict.CONFIRMED_EXISTING_DEFECT, (41, 42, 42, 42)),
])
def test_confirmed_cases(case: str, verdict: c.Verdict, chain: tuple[int, ...]) -> None:
    receipt = simulate_case(case).receipt
    assert receipt.verdict is verdict
    probe = receipt.probes[(receipt.counterexample_probe_no or 0) - 1]
    assert probe.confirmation is not None
    assert (probe.oracle.expected_cents, probe.base.value, probe.head.value,
            probe.confirmation.value) == chain
    assert probe.confirmation.attempt_id != probe.head.attempt_id
    assert receipt.replay is not None
    assert reference.validate_replay(receipt, None) == receipt.replay
    assert catalog.PR_CASES[case].expected_verdict == verdict.value


def test_fixed_case_bounded_clean_coverage() -> None:
    receipt = simulate_case("fixed").receipt
    assert receipt.verdict is c.Verdict.NO_DEFECT_FOUND
    assert receipt.coverage.complete
    assert receipt.coverage.distinct_usable_pairs >= 3
    assert receipt.replay is None
    assert receipt.budget.used.sandbox_invocations <= 15


def test_flaky_confirmation_is_inconclusive() -> None:
    runner = adapters.FixtureRunner(faults={(c.Side.CONFIRM, None): 41})
    receipt = simulate_case("buggy", runner=runner).receipt
    assert receipt.verdict is c.Verdict.INCONCLUSIVE
    assert receipt.reason_code is c.ReasonCode.NONDETERMINISTIC


def test_head_timeout_is_excluded_without_update() -> None:
    runner = adapters.FixtureRunner(faults={(c.Side.HEAD, None): c.ObservationOutcome.TIMEOUT})
    result = simulate_case("fixed", runner=runner)
    assert all(p.exclusion_reason is c.ExclusionReason.TIMEOUT and p.posterior is None
               for p in result.probes)
    assert result.receipt.verdict is c.Verdict.INCONCLUSIVE


def test_meta_failure_uses_labeled_builtin_plan() -> None:
    meta = adapters.FixtureMeta(fail_with=c.ReasonCode.PROVIDER_UNAVAILABLE)
    receipt = simulate_case("buggy", meta=meta).receipt
    assert receipt.providers[c.ProviderName.META].mode is c.ProviderMode.FALLBACK
    assert receipt.hypotheses is not None
    assert receipt.hypotheses.plan_source is c.PlanSource.BUILTIN_FALLBACK
    assert receipt.verdict is c.Verdict.CONFIRMED_REGRESSION


def test_isolation_unverified_runs_nothing() -> None:
    runner = adapters.FixtureRunner(isolation=c.IsolationStatus.FAILED)
    result = simulate_case("buggy", runner=runner)
    assert runner.requests == []
    assert result.receipt.verdict is c.Verdict.INCONCLUSIVE
    assert result.receipt.reason_code is c.ReasonCode.ISOLATION_UNVERIFIED


def test_route_fixture_modes() -> None:
    route = adapters.FixtureRoute()
    state = c.DecisionState.model_validate_json(
        (REPO / "schemas" / "samples" / "DecisionState.json").read_bytes())
    first = asyncio.run(route.choose(state, tuple(c.Strategy)))
    assert first.mode is c.RouteMode.STUB
    late = asyncio.run(route.choose(state.model_copy(update={"step": 3}), tuple(c.Strategy)))
    assert (late.mode, late.reason_code, late.choice) == (
        c.RouteMode.POLICY, c.RouteReasonCode.CALL_BUDGET, c.Strategy.CONSTRAINT)
    single = asyncio.run(route.choose(state, (c.Strategy.BOUNDARY,)))
    assert single.reason_code is c.RouteReasonCode.SINGLE_ELIGIBLE


def test_golden_receipts_verify_and_reject_tamper() -> None:
    for case in ("buggy", "fixed"):
        text = (catalog.DEMO_DIR / "receipts" / f"{case}.seed17.json").read_text()
        receipt = reference.verify_receipt(text)
        assert receipt.execution_mode is c.ExecutionMode.OFFLINE_FIXTURE
        tampered = json.loads(text)
        tampered["probes"][0]["head"]["value"] += 1
        with pytest.raises(ports.AdapterError) as error:
            reference.verify_receipt(json.dumps(tampered))
        assert error.value.code is c.ReasonCode.RECEIPT_TAMPERED


def test_enrollment_example_matches_catalog() -> None:
    registry = c.EnrollmentRegistry.model_validate_json(
        (REPO / "config" / "enrollments.example.json").read_bytes())
    assert registry.enrollments == [catalog.demo_enrollment()]
    manifest = json.loads((catalog.DEMO_DIR / "manifest.json").read_text())
    assert manifest["contract_sha256"] == catalog.contract_sha256()


def test_provider_examples_are_consistent() -> None:
    providers = catalog.PROVIDERS_DIR
    response = json.loads((providers / "meta_response.json").read_text())
    content = response["choices"][0]["message"]["content"]
    c.HypothesisPlanPayload.model_validate_json(content)
    snap = json.loads((providers / "snap_response.json").read_text())
    request = json.loads((providers / "snap_request.json").read_text())
    offered = set(request["questions"]["next_action"]["criteria"])
    assert snap["answers"]["next_action"]["choice"] in offered
    contents = json.loads((providers / "github_contents_head.json").read_text())
    import base64

    data = base64.b64decode(contents["content"])
    assert c.git_blob_sha1(data) == contents["sha"]
    tree = json.loads((providers / "github_tree_head.json").read_text())
    assert tree["tree"][0]["sha"] == contents["sha"]
    assert tree["tree"][0]["mode"] == "100644"


def test_in_memory_store_idempotency_quota_and_fencing() -> None:
    store = adapters.InMemoryRunStore()
    validator = adapters.FixtureRepositoryValidator()
    url = catalog.PR_CASES["buggy"].url
    target, enrollment = validator.resolve(url)
    request = c.RunRequest(pr_url=url, seed=17)
    first = store.create_run(request, idempotency_key="k1", requester_scope="acp:a",
                             target=target, enrollment=enrollment)
    again = store.create_run(request, idempotency_key="k1", requester_scope="acp:a",
                             target=target, enrollment=enrollment)
    assert first.run_id == again.run_id
    with pytest.raises(ports.IdempotencyConflictError):
        store.create_run(c.RunRequest(pr_url=url, seed=18), idempotency_key="k1",
                         requester_scope="acp:a", target=target, enrollment=enrollment)
    with pytest.raises(ports.AdapterError) as error:  # one active run per requester bucket
        store.create_run(request, idempotency_key="k2", requester_scope="acp:a", target=target,
                         enrollment=enrollment)
    assert error.value.code is c.ReasonCode.RATE_LIMITED
    lease = store.claim("worker-1", 30)
    assert lease is not None
    assert store.claim("worker-2", 30) is None
    stale = lease.model_copy(update={"epoch": lease.epoch + 1})
    event = c.RunEvent(run_id=lease.run_id, seq=1, at=adapters.FIXTURE_EPOCH,
                       kind=c.RunEventKind.STATE_CHANGED, from_state=c.RunState.QUEUED,
                       to_state=c.RunState.FETCHING, reason_code=None, detail=None)
    with pytest.raises(ports.LeaseLostError):
        store.checkpoint(stale, event)
    store.checkpoint(lease, event)
    status = store.get_status(lease.run_id)
    assert status is not None
    assert status.status is c.RunState.FETCHING


@pytest.mark.parametrize("url", [
    "http://github.com/proofhunter-demo/checkout-demo/pull/1",
    "https://github.com/proofhunter-demo/checkout-demo/pull/1?x=1",
    "https://user@github.com/proofhunter-demo/checkout-demo/pull/1",
    "https://github.com/proofhunter-demo/checkout-demo/pull/0",
    "https://github.com/proofhunter-demo/checkout-demo/pull/1/files",
])
def test_fixture_validator_rejects_noncanonical(url: str) -> None:
    with pytest.raises(ports.AdapterError) as error:
        adapters.FixtureRepositoryValidator().resolve(url)
    assert error.value.code is c.ReasonCode.INVALID_PR_URL


def test_fixture_validator_rejects_unenrolled() -> None:
    with pytest.raises(ports.AdapterError) as error:
        adapters.FixtureRepositoryValidator().resolve("https://github.com/evil/repo/pull/1")
    assert error.value.code is c.ReasonCode.REPO_NOT_ENROLLED


def test_webhook_fixture_signature() -> None:
    verifier = adapters.FixtureWebhookVerifier()
    body = b'{"action":"opened"}'
    assert verifier.verify(body, verifier.sign(body))
    assert not verifier.verify(body, "sha256=" + "0" * 64)
    assert not verifier.verify(body, None)


def test_checks_fixture_never_publishes() -> None:
    sample = c.CheckIntent.model_validate_json(
        (REPO / "schemas" / "samples" / "CheckIntent.json").read_bytes())
    result = asyncio.run(adapters.FixtureChecks().publish(sample))
    assert result.status is c.PublishStatus.SKIPPED_OFFLINE
    assert result.check_run_id is None
