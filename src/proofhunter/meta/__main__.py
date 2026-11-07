"""Operator smoke entry point; dry-run is always network-free."""

import argparse
import asyncio
import json

from fixtures import catalog
from fixtures.adapters import FixtureRepositoryValidator, FixtureSource
from proofhunter.config import load_settings
from proofhunter.contracts import PlanningContext, ReasonCode
from proofhunter.meta import MetaError, build_request, create_meta_port


async def demo_context() -> PlanningContext:
    target, enrollment = FixtureRepositoryValidator().resolve(catalog.PR_CASES["buggy"].url)
    source = FixtureSource()
    return (await source.fetch(await source.pin(target), enrollment)).context


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--live", action="store_true")
    args = parser.parse_args()
    settings = load_settings() if args.live else load_settings({"PROOFHUNTER_PROFILE": "offline"})
    if args.live and settings.offline:
        parser.error("--live requires PROOFHUNTER_PROFILE=live")
    context = await demo_context()
    if not args.live:
        print(json.dumps(build_request(context, settings.meta.model), sort_keys=True))
        return
    # A smoke gate spends exactly one HTTP request, even on a transient failure.
    async def no_retry(_: float) -> None:
        raise MetaError(ReasonCode.PROVIDER_UNAVAILABLE, attempts=1)
    try:
        result = await create_meta_port(settings, sleep=no_retry).propose(context)
    except MetaError as error:
        print(json.dumps({"provider": "meta", "reason_code": error.code.value,
                          "attempts": error.attempts}))
        raise SystemExit(1) from None
    print(result.provenance.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
