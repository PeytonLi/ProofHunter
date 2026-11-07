"""Private offline command entry points; live commands require operator review."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from pydantic import SecretStr
from starlette.responses import JSONResponse, Response

from proofhunter import contracts as c
from proofhunter import oracle
from proofhunter.config import (
    ServiceRole,
    Settings,
    github_private_key_pem,
    load_enrollments,
    load_settings,
    mode_config_fingerprint,
)
from proofhunter.pipeline import VerificationPipeline, WallClock, compose
from proofhunter.ports import AdapterError, ApiDependencies, ConfigError
from proofhunter.runner.approval import config_fingerprint, harness_sha256, local_approval_refusal
from proofhunter.store import create_store
from proofhunter.worker import VerificationWorker

UNPROVISIONED_SNAPSHOT = "runner-unprovisioned"
UNPROVISIONED_IMAGE = "sha256:" + "0" * 64
FIXTURE_IMAGE = "sha256:f19b56e7023eb77f7882ec2559faf5b031ddda0d4e0721b6fefa92808b3dd6b0"
CONTRACT_SHA256 = "f608d4b8c47ee74e1ef0fdc1f4d62c54f19d054489d4a5787809ebb1bccdbfa8"


def live_trust_gate(settings: Settings, enrollments: Sequence[c.Enrollment]) -> None:
    if settings.github.enable_checks or settings.github.enable_webhook:
        raise ConfigError("checks and webhooks are not approved")
    if not enrollments:
        raise ConfigError("enrollment required")
    for enrollment in enrollments:
        if (
            enrollment.repo_id == 900000001
            or enrollment.installation_id == 900000002
            or enrollment.repo_full_name.lower() == "proofhunter-demo/checkout-demo"
            or enrollment.snapshot_id == "proofhunter-offline-fixture-v1"
            or enrollment.image_digest == FIXTURE_IMAGE
        ):
            raise ConfigError("fixture enrollment forbidden")
        if settings.daytona.enable_live_runner and (
            enrollment.snapshot_id == UNPROVISIONED_SNAPSHOT
            or enrollment.image_digest == UNPROVISIONED_IMAGE
            or settings.daytona.snapshot != enrollment.snapshot_id
            or settings.daytona.image_digest != enrollment.image_digest
        ):
            raise ConfigError("runner provisioning required")
    if not settings.offline and settings.daytona.enable_live_runner:
        harness = harness_sha256()
        fingerprint = config_fingerprint(
            settings.daytona.snapshot or "",
            settings.daytona.image_digest or "",
            harness,
            c.canonical_sha256(str(settings.db_path.resolve()))[:24],
        )
        reason = local_approval_refusal(
            settings.db_path.parent,
            fingerprint=fingerprint,
            image_digest=settings.daytona.image_digest or "",
        )
        if reason is not None:
            raise ConfigError(reason)


def live_settings(env: dict[str, str], role: ServiceRole) -> Settings:
    settings = load_settings(env, role=role)
    raw = env.get("PROOFHUNTER_ENROLLMENTS_JSON")
    if raw is not None:
        materialize_enrollments(settings, raw)
    live_trust_gate(settings, load_enrollments(settings.enrollments_path))
    return settings


def materialize_enrollments(settings: Settings, raw: str) -> None:
    registry = c.EnrollmentRegistry.model_validate_json(raw)
    live_trust_gate(settings, [item for item in registry.enrollments if item.enabled])
    path = settings.enrollments_path
    if path.is_symlink() or path.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise ConfigError("inline enrollment requires external regular path")
    descriptor, temporary = tempfile.mkstemp(prefix=".enrollment-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(registry.model_dump_json(indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def live_app(settings: Settings) -> FastAPI:
    from proofhunter.api import create_app
    from proofhunter.github import create_repository_validator

    enrollments = load_enrollments(settings.enrollments_path)
    store = create_store(settings)
    app = create_app(
        ApiDependencies(
            settings,
            store,
            create_repository_validator(settings, enrollments),
            None,
            WallClock(),
            heartbeat_store=store,
            config_fingerprint=mode_config_fingerprint(settings, enrollments),
        )
    )
    if not settings.enable_intake:

        @app.middleware("http")
        async def refuse_intake(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            path = request.url.path.rstrip("/")
            if request.method == "POST" and (
                path == "/v1/runs" or (path.startswith("/v1/runs/") and path.endswith("/replays"))
            ):
                return JSONResponse(
                    {
                        "error": {
                            "code": "intake_disabled",
                            "message": "intake disabled",
                            "request_id": uuid4().hex,
                            "retryable": False,
                        }
                    },
                    status_code=503,
                )
            return await call_next(request)

    return app


def live_worker(settings: Settings, args: argparse.Namespace) -> VerificationWorker:
    store = create_store(settings)
    return VerificationWorker(
        compose(settings, store),
        heartbeat_store=store,
        config_fingerprint=mode_config_fingerprint(
            settings, load_enrollments(settings.enrollments_path)
        ),
        profile=c.Profile.LIVE,
        retention_dry_run=(not args.retention_apply)
        if args.retention_apply or args.retention_dry_run
        else None,
        retention_hours=args.retention_hours,
    )


async def supervise(api_settings: Settings, worker: VerificationWorker) -> None:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            live_app(api_settings),
            host=api_settings.bind_host,
            port=api_settings.bind_port,
            log_level="critical",
            access_log=False,
        )
    )
    tasks = [asyncio.create_task(server.serve()), asyncio.create_task(worker.poll())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise RuntimeError("stack component stopped")
    finally:
        server.should_exit = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def repository_metadata(settings: Settings, repo_url: str) -> tuple[int, int, str]:
    """App-auth discovery, then repository-scoped installation authentication."""
    import jwt

    from proofhunter.github import _session, parse_pr_url
    from proofhunter.github.client import obj, positive

    ref = parse_pr_url(repo_url + "/pull/1")
    now = int(WallClock().now().timestamp())
    credential = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(settings.github.app_id)},
        github_private_key_pem(settings).get_secret_value(),
        algorithm="RS256",
    )
    session = _session(settings)
    try:
        installation = obj(
            await session.http.request(
                "GET",
                f"/repos/{ref.full_name}/installation",
                credential,
            )
        )
        installation_id = positive(installation["id"])
        bootstrap = obj(
            await session.http.request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                credential,
                body={
                    "repositories": [ref.repo],
                    "permissions": {
                        "contents": "read",
                        "pull_requests": "read",
                        "metadata": "read",
                    },
                },
            )
        )
        token = bootstrap["token"]
        if (
            not isinstance(token, str)
            or not token
            or not token.isascii()
            or any(ord(char) <= 32 for char in token)
        ):
            raise ValueError("invalid installation credential")
        data = obj(await session.http.request("GET", f"/repos/{ref.full_name}", token))
        repo_id = positive(data["id"])
        full_name = data["full_name"]
        if not isinstance(full_name, str) or full_name.lower() != ref.full_name:
            raise ValueError("repository identity mismatch")
        target = c.PrTarget(**ref.model_dump(), repo_id=repo_id, installation_id=installation_id)
        verified = obj(await session.request(target, "GET", f"/repos/{ref.full_name}"))
        if (
            positive(verified["id"]) != repo_id
            or verified["private"] is not False
            or verified["full_name"] != full_name
            or verified.get("archived") is not False
        ):
            raise ValueError("repository is not eligible")
        return repo_id, installation_id, full_name
    finally:
        await session.aclose()


def enroll(settings: Settings, args: argparse.Namespace) -> None:
    destination = args.out.resolve()
    repo_root = Path(__file__).resolve().parents[2]
    if destination.is_relative_to(repo_root):
        raise ValueError("enrollment must be outside repository")
    if args.unprovisioned_runner:
        if args.snapshot_id or args.image_digest:
            raise ValueError("conflicting runner arguments")
        snapshot, image = UNPROVISIONED_SNAPSHOT, UNPROVISIONED_IMAGE
    else:
        snapshot, image = args.snapshot_id, args.image_digest
        if (
            not snapshot
            or not image
            or snapshot == UNPROVISIONED_SNAPSHOT
            or image == UNPROVISIONED_IMAGE
        ):
            raise ValueError("runner pins required")
    if snapshot == "proofhunter-offline-fixture-v1" or image == FIXTURE_IMAGE:
        raise ValueError("fixture runner forbidden")
    repo_id, installation_id, full_name = asyncio.run(repository_metadata(settings, args.repo_url))
    enrollment = c.Enrollment(
        schema_version=c.SCHEMA_VERSION,
        repo_id=repo_id,
        installation_id=installation_id,
        repo_full_name=full_name,
        visibility="public",
        source_path=c.SOURCE_PATH,
        entrypoint=c.ENTRYPOINT,
        contract_id=c.CONTRACT_ID,
        contract_sha256=CONTRACT_SHA256,
        oracle_version=c.ORACLE_VERSION,
        harness_version=c.HARNESS_VERSION,
        harness_sha256=harness_sha256(),
        snapshot_id=snapshot,
        image_digest=image,
        enabled=True,
    )
    live_trust_gate(settings, [enrollment])
    registry = c.EnrollmentRegistry(schema_version=c.SCHEMA_VERSION, enrollments=[enrollment])
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(registry.model_dump_json(indent=2) + "\n")
    print(
        f"live enrolled {full_name} {snapshot} "
        f"enrollment_fingerprint={c.enrollment_fingerprint(enrollment)}"
    )


def live_main(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    env["PROOFHUNTER_PROFILE"] = "live"
    if getattr(args, "intake_disabled", False):
        env["PROOFHUNTER_ENABLE_INTAKE"] = "false"
    role = ServiceRole.API if args.command == "serve" else ServiceRole.WORKER
    if args.command == "enroll":
        settings = load_settings(env, role=role)
        enroll(settings, args)
        return 0
    settings = live_settings(env, role)
    if args.command in ("serve", "stack") and not settings.daytona.enable_live_runner:
        print(
            "runner disabled: isolation_unverified; intake "
            + ("enabled" if settings.enable_intake else "disabled"),
            flush=True,
        )
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            live_app(settings),
            host=settings.bind_host,
            port=settings.bind_port,
            log_level="critical",
            access_log=False,
        )
        return 0
    if args.command in ("worker", "stack"):
        worker = live_worker(settings, args)
        if args.command == "stack":
            api_settings = live_settings(env, ServiceRole.API)
            enrollments = load_enrollments(settings.enrollments_path)
            if mode_config_fingerprint(api_settings, enrollments) != mode_config_fingerprint(
                settings, enrollments
            ):
                raise ConfigError("role fingerprints differ")
            asyncio.run(supervise(api_settings, worker))
        else:
            asyncio.run(worker.poll())
        return 0
    store = create_store(settings)
    if args.command == "retention":
        print(json.dumps(store.purge_expired(WallClock().now(), dry_run=not args.apply)))
        return 0
    if args.command == "demo":
        from proofhunter.github import create_repository_validator

        if not args.pr_url or not args.base or not args.head:
            raise ValueError("live demo requires PR URL and expected pins")
        from pydantic import TypeAdapter

        pins = (
            TypeAdapter(c.Sha40).validate_python(args.base),
            TypeAdapter(c.Sha40).validate_python(args.head),
        )
        request = c.RunRequest(pr_url=args.pr_url, seed=args.seed)
        target, enrollment = create_repository_validator(
            settings, load_enrollments(settings.enrollments_path)
        ).resolve(request.pr_url)
        pipeline = compose(settings, store)
        pipeline.expected_pins = pins
        status = store.create_run(
            request,
            idempotency_key=str(uuid4()),
            requester_scope="live-demo",
            target=target,
            enrollment=enrollment,
        )
        asyncio.run(VerificationWorker(pipeline).run_once())
        receipt = store.get_receipt(status.run_id)
        if receipt is None:
            raise ValueError("receipt unavailable")
        args.receipt.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
        if (
            receipt.pinned_revision is None
            or (receipt.pinned_revision.base_sha, receipt.pinned_revision.head_sha) != pins
        ):
            raise AdapterError(
                "github", c.ReasonCode.REVISION_UNAVAILABLE, "pins differ", retryable=False
            )
        print(f"live {receipt.verdict}: {receipt.reason_code}")
        return 0
    raw = args.receipt.read_bytes()
    receipt = oracle.verify_receipt(raw)
    if receipt.execution_mode is not c.ExecutionMode.LIVE:
        raise ValueError("receipt mode mismatch")
    if receipt.reason_code is c.ReasonCode.ISOLATION_UNVERIFIED:
        raise AdapterError(
            "runner", c.ReasonCode.ISOLATION_UNVERIFIED, "isolation unverified", retryable=False
        )
    if receipt.counterexample_probe_no is None or receipt.replay is None:
        raise AdapterError(
            "runner", c.ReasonCode.REPLAY_UNAVAILABLE, "replay unavailable", retryable=False
        )
    pipeline = compose(settings, store)
    number = None if args.probe == "counterexample" else int(args.probe)
    asyncio.run(replay_receipt(pipeline, raw, number))
    print("live replay reproduced")
    return 0


async def replay_receipt(pipeline: VerificationPipeline, raw: bytes, number: int | None) -> None:
    receipt = oracle.verify_receipt(raw)
    replay = oracle.validate_replay(receipt, number)
    if replay.execution_mode is not (
        c.ExecutionMode.OFFLINE_FIXTURE if pipeline.settings.offline else c.ExecutionMode.LIVE
    ):
        raise ValueError("replay execution mode mismatch")
    revision = receipt.pinned_revision
    if revision is None:
        raise ValueError("missing source binding")
    enrollment = pipeline.enrollment
    if not pipeline.settings.offline and (
        receipt.enrollment_fingerprint != c.enrollment_fingerprint(enrollment)
    ):
        raise AdapterError(
            "runner",
            c.ReasonCode.REPLAY_UNAVAILABLE,
            "enrollment binding mismatch",
            retryable=False,
        )
    pin = c.PinnedRevision(**revision.model_dump(), installation_id=enrollment.installation_id)
    bundle = await pipeline.source.fetch(pin, enrollment)
    if (
        bundle.base.content_sha256 != replay.base_source_sha256
        or bundle.head.content_sha256 != replay.head_source_sha256
    ):
        raise ValueError("missing reviewed source artifact")
    attestation = await pipeline.runner.attest()
    if attestation.reason_code is not None:
        raise ValueError("isolation unverified")
    probe = receipt.probes[replay.probe_no - 1]
    expected = oracle.evaluate(enrollment, probe.candidate)
    if expected.status is not c.OracleStatus.OK:
        raise ValueError("oracle unavailable")
    observations = []
    for side in (c.Side.BASE, c.Side.HEAD, c.Side.CONFIRM):
        source = bundle.base if side is c.Side.BASE else bundle.head
        request = c.ExecutionRequest(
            run_id=receipt.run_id,
            attempt_id=str(uuid4()),
            side=side,
            commit_sha=source.commit_sha,
            source_path=enrollment.source_path,
            source_content=source.content,
            source_sha256=source.content_sha256,
            input=replay.input,
            input_sha256=replay.input_sha256,
            entrypoint=enrollment.entrypoint,
            resources=c.ResourcePolicy(),
            snapshot_id=replay.snapshot_id,
            image_digest=replay.image_digest,
            harness_version=replay.harness_version,
            harness_sha256=replay.harness_sha256,
        )
        observation = await pipeline.runner.observe(request)
        if not oracle.check_replay_observation(replay, observation, side):
            raise ValueError("replay observation binding mismatch")
        observations.append(observation)
    assessment = oracle.assess_probe(expected, *observations, binding=oracle.replay_binding(replay))
    if (
        assessment.evidence_kind != probe.evidence_kind
        or observations[0].value != probe.base.value
        or observations[1].value != probe.head.value
        or (probe.confirmation is not None and observations[2].value != probe.confirmation.value)
    ):
        raise ValueError("replay did not reproduce")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proofhunter")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("demo", "replay", "serve", "worker", "stack", "enroll", "retention"):
        sub = commands.add_parser(command)
        sub.add_argument("--profile", choices=("offline", "live"), default="offline")
        if command in ("serve", "stack"):
            sub.add_argument("--intake-disabled", action="store_true")
        if command in ("demo", "replay"):
            sub.add_argument("--offline", action="store_true")
            sub.add_argument("--receipt", type=Path, required=True)
        if command == "demo":
            sub.add_argument("--case", choices=("buggy", "fixed", "half_even", "existing"))
            sub.add_argument("--seed", type=int, default=17)
            sub.add_argument("--pr-url")
            sub.add_argument("--base")
            sub.add_argument("--head")
        elif command == "replay":
            sub.add_argument("--probe", default="counterexample")
        elif command == "enroll":
            sub.add_argument("--repo-url", required=True)
            sub.add_argument("--out", type=Path, required=True)
            sub.add_argument("--snapshot-id")
            sub.add_argument("--image-digest")
            sub.add_argument("--unprovisioned-runner", action="store_true")
        elif command == "retention":
            sub.add_argument("--apply", action="store_true")
        if command in ("worker", "stack"):
            retention = sub.add_mutually_exclusive_group()
            retention.add_argument("--retention-apply", action="store_true")
            retention.add_argument("--retention-dry-run", action="store_true")
            sub.add_argument("--retention-hours", type=int, choices=range(1, 169), default=24)
    args = parser.parse_args(argv)
    try:
        if args.profile != "offline":
            return live_main(args)
        if args.command == "enroll" or args.command == "stack":
            raise ValueError("live profile required")
        env = dict(os.environ)
        env["PROOFHUNTER_PROFILE"] = "offline"
        with tempfile.TemporaryDirectory(prefix="proofhunter-demo-") as temporary:
            if args.command in ("demo", "replay") and not env.get("PROOFHUNTER_DB_PATH"):
                env["PROOFHUNTER_DB_PATH"] = str(Path(temporary) / "runs.sqlite3")
            settings = load_settings(env)
            store = create_store(settings)
            if args.command == "retention":
                print(json.dumps(store.purge_expired(WallClock().now(), dry_run=not args.apply)))
                return 0
            pipeline = compose(settings, store)
            fingerprint = mode_config_fingerprint(
                settings, load_enrollments(settings.enrollments_path)
            )
            worker = VerificationWorker(
                pipeline,
                heartbeat_store=store,
                config_fingerprint=fingerprint,
                profile=c.Profile.OFFLINE,
                retention_dry_run=(not args.retention_apply)
                if args.command == "worker" and (args.retention_apply or args.retention_dry_run)
                else None,
                retention_hours=args.retention_hours if args.command == "worker" else 24,
            )
            if args.command == "demo":
                from fixtures.adapters import FixtureRepositoryValidator

                numbers = {"buggy": 1, "fixed": 2, "half_even": 3, "existing": 4}
                if args.case is None:
                    raise ValueError("offline demo requires case")
                request = c.RunRequest(
                    pr_url=f"https://github.com/proofhunter-demo/checkout-demo/pull/{numbers[args.case]}",
                    seed=args.seed,
                )
                target, enrollment = FixtureRepositoryValidator().resolve(request.pr_url)
                status = store.create_run(
                    request,
                    idempotency_key=str(uuid4()),
                    requester_scope="offline-demo",
                    target=target,
                    enrollment=enrollment,
                )
                asyncio.run(worker.run_once())
                receipt = store.get_receipt(status.run_id)
                if receipt is None:
                    raise ValueError("worker did not produce a terminal receipt")
                args.receipt.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
                print(f"offline_fixture {receipt.verdict}: {receipt.reason_code}")
                return 0 if receipt.verdict is not None else 1
            if args.command == "replay":
                number = None if args.probe == "counterexample" else int(args.probe)
                asyncio.run(replay_receipt(pipeline, args.receipt.read_bytes(), number))
                print("offline_fixture replay reproduced")
            elif args.command == "worker":
                asyncio.run(worker.poll())
            else:
                import uvicorn

                from fixtures.adapters import FixtureRepositoryValidator
                from proofhunter.api import create_app

                # Offline settings never read the live transport tokens.
                api = env.get("PROOFHUNTER_TEST_API_TOKEN")
                operator = env.get("PROOFHUNTER_TEST_OPERATOR_TOKEN")
                if not api or not operator or api == operator:
                    raise ValueError("offline serve requires distinct explicit test-only tokens")
                settings = settings.model_copy(
                    update={"api_token": SecretStr(api), "operator_token": SecretStr(operator)}
                )
                app = create_app(
                    ApiDependencies(
                        settings,
                        store,
                        FixtureRepositoryValidator(),
                        None,
                        WallClock(),
                        heartbeat_store=store,
                        config_fingerprint=fingerprint,
                    )
                )
                uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)
        return 0
    except Exception as error:
        reason = error.code.value if isinstance(error, AdapterError) else type(error).__name__
        label = "live" if args.profile == "live" else "offline_fixture"
        print(f"{label} refused: {reason}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
