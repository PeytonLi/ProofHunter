"""Isolated runner; offline never constructs a Daytona client."""

from proofhunter.config import Settings
from proofhunter.ports import RunnerPort
from proofhunter.runner.daytona import DaytonaRunner
from proofhunter.runner.offline import ReviewedFixtureRunner


def create_runner_port(settings: Settings) -> RunnerPort:
    if settings.offline:
        return ReviewedFixtureRunner()
    return DaytonaRunner.from_settings(settings)


__all__ = ["DaytonaRunner", "ReviewedFixtureRunner", "create_runner_port"]
