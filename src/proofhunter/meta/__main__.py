"""Operator smoke entry point; dry-run is always network-free."""

import argparse
import asyncio
import json
import uuid

from proofhunter.config import load_settings
from proofhunter.contracts import (
    MAX_PROPOSALS,
    ContextFile,
    HypothesisMode,
    PlanningContext,
)
from proofhunter.meta import MetaError, build_request, create_meta_port

# Public demo head (buggy per-line rounding); smoke uses only this public text.
DEMO_SOURCE = (
    '"""Demo checkout module, buggy head: discounts and rounds each line separately."""\n'
    "\n\n"
    "def total_cents(prices_cents: list[int], discount_percent: int) -> int:\n"
    "    total = 0\n"
    "    for price in prices_cents:\n"
    "        total += (price * (100 - discount_percent) + 50) // 100\n"
    "    return total\n"
)
DEMO_CONTRACT = (
    "checkout.total_cents(prices_cents: list[int], discount_percent: int) -> int. "
    "0-8 items of 0-10000 integer cents, integer discount 0-100. Round the discounted "
    "subtotal once, half up: n = sum(prices_cents) * (100 - discount_percent); "
    "return (n + 50) // 100."
)


def demo_context() -> PlanningContext:
    return PlanningContext(
        run_id=str(uuid.uuid4()), contract_id="checkout-rounding@1",
        entrypoint="checkout.total_cents", contract_text=DEMO_CONTRACT,
        files=[ContextFile(path="checkout.py", start_line=1, text=DEMO_SOURCE)],
        diff="", supported_modes=list(HypothesisMode), max_hypotheses=MAX_PROPOSALS)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--live", action="store_true")
    args = parser.parse_args()
    settings = load_settings() if args.live else load_settings({"PROOFHUNTER_PROFILE": "offline"})
    if args.live and settings.offline:
        parser.error("--live requires PROOFHUNTER_PROFILE=live")
    context = demo_context()
    if not args.live:
        print(json.dumps(build_request(context, settings.meta.model), sort_keys=True))
        return
    # A smoke gate spends exactly one HTTP request, even on a transient failure.
    try:
        result = await create_meta_port(settings, max_attempts=1).propose(context)
    except MetaError as error:
        print(json.dumps({"provider": "meta", "reason_code": error.code.value,
                          "attempts": error.attempts}))
        raise SystemExit(1) from None
    print(result.provenance.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
