"""Research CLI (plan section 16).

Each command dispatches to ``<module>:<function>(argv) -> int`` owned by a lane (see
OWNERSHIP.md). Until that module exists the command exits non-zero with "not implemented".
Run: ``uv run --project research --locked python -m research.cli <command> [args...]``.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Sequence
from typing import Final, NamedTuple, cast


class Command(NamedTuple):
    lane: str
    description: str
    module: str
    function: str


COMMANDS: Final[dict[str, Command]] = {
    "inspect-dataset": Command(
        "dataset", "Inspect/validate dataset", "research.data.commands", "inspect_dataset"
    ),
    "prepare-splits": Command(
        "dataset", "Prepare grouped splits", "research.data.commands", "prepare_splits"
    ),
    "run-pilot": Command(
        "integration", "Run reproduction pilot", "research.execution.commands", "run_pilot"
    ),
    "collect-observations": Command(
        "ranker",
        "Collect ranking observations",
        "research.ranker.commands",
        "collect_observations",
    ),
    "prepare-generator-examples": Command(
        "generator",
        "Prepare generator examples",
        "research.generator.commands",
        "prepare_generator_examples",
    ),
    "train-ranker": Command("ranker", "Train ranker", "research.ranker.commands", "train_ranker"),
    "train-generator": Command(
        "generator", "Train generator", "research.training.commands", "train_generator"
    ),
    "evaluate": Command(
        "integration", "Evaluate frozen checkpoints", "research.execution.commands", "evaluate"
    ),
    "report": Command("measurement", "Produce reports", "research.reporting.commands", "report"),
    "verify-teardown": Command(
        "measurement", "Verify teardown", "research.metrics.commands", "verify_teardown"
    ),
}
NOT_IMPLEMENTED_EXIT: Final = 3
USAGE_EXIT: Final = 2

Handler = Callable[[Sequence[str]], int]


def resolve(command: str) -> Handler | None:
    spec = COMMANDS[command]
    try:
        module = importlib.import_module(spec.module)
    except ModuleNotFoundError as exc:
        if exc.name is not None and spec.module.startswith(exc.name):
            return None
        raise
    handler = getattr(module, spec.function, None)
    return cast(Handler, handler) if callable(handler) else None


def usage() -> str:
    lines = ["usage: research <command> [args...]", "", "commands:"]
    lines += [f"  {name:28} {c.description} (owner: {c.lane})" for name, c in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(usage(), file=sys.stderr if not args else sys.stdout)
        return USAGE_EXIT if not args else 0
    if args[0] not in COMMANDS:
        print(f"unknown command: {args[0]}\n{usage()}", file=sys.stderr)
        return USAGE_EXIT
    handler = resolve(args[0])
    if handler is None:
        spec = COMMANDS[args[0]]
        print(
            f"not implemented: {args[0]} ({spec.description}); owner: {spec.lane} lane "
            f"({spec.module}:{spec.function})",
            file=sys.stderr,
        )
        return NOT_IMPLEMENTED_EXIT
    return handler(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
