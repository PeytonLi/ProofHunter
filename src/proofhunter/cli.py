"""Private offline command entry points; live commands require operator review."""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

from proofhunter import contracts as c
from proofhunter import oracle
from proofhunter.config import load_settings
from proofhunter.pipeline import VerificationPipeline, WallClock, compose
from proofhunter.ports import AdapterError, ApiDependencies, ConfigError
from proofhunter.store import create_store
from proofhunter.worker import VerificationWorker


async def replay_receipt(pipeline: VerificationPipeline, raw: bytes, number: int | None) -> None:
    receipt = oracle.verify_receipt(raw)
    replay = oracle.validate_replay(receipt, number)
    if (
        replay.execution_mode is not c.ExecutionMode.OFFLINE_FIXTURE
        or not pipeline.settings.offline
    ):
        raise ValueError("replay execution mode mismatch")
    revision = receipt.pinned_revision
    if revision is None:
        raise ValueError("missing source binding")
    enrollment = pipeline.enrollment
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
    for command in ("demo", "replay", "serve", "worker"):
        sub = commands.add_parser(command)
        sub.add_argument("--profile", choices=("offline", "live"), default="offline")
        if command in ("demo", "replay"):
            sub.add_argument("--offline", action="store_true")
            sub.add_argument("--receipt", type=Path, required=True)
        if command == "demo":
            sub.add_argument(
                "--case", choices=("buggy", "fixed", "half_even", "existing"), required=True
            )
            sub.add_argument("--seed", type=int, default=17)
        elif command == "replay":
            sub.add_argument("--probe", default="counterexample")
    args = parser.parse_args(argv)
    try:
        if args.profile != "offline":
            raise ValueError("live CLI is operator-gated; not enabled in this offline build")
        env = dict(os.environ)
        env["PROOFHUNTER_PROFILE"] = "offline"
        with tempfile.TemporaryDirectory(prefix="proofhunter-demo-") as temporary:
            if args.command in ("demo", "replay") and not env.get("PROOFHUNTER_DB_PATH"):
                env["PROOFHUNTER_DB_PATH"] = str(Path(temporary) / "runs.sqlite3")
            settings = load_settings(env)
            store = create_store(settings)
            pipeline = compose(settings, store)
            if args.command == "demo":
                from fixtures.adapters import FixtureRepositoryValidator

                numbers = {"buggy": 1, "fixed": 2, "half_even": 3, "existing": 4}
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
                asyncio.run(VerificationWorker(pipeline).run_once())
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
                asyncio.run(VerificationWorker(pipeline).poll())
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
                        settings, store, FixtureRepositoryValidator(), None, WallClock()
                    )
                )
                uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)
        return 0
    except (AdapterError, ConfigError, ValueError, OSError) as error:
        reason = error.code.value if isinstance(error, AdapterError) else type(error).__name__
        print(f"offline_fixture refused: {reason}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
