"""Bounded live GitHub App source smoke: pin and fetch exact PR revisions.

Operator-only. Requires ``PROOFHUNTER_PROFILE=live`` and loads worker-role settings,
so the App key comes from ``GITHUB_PRIVATE_KEY`` (in memory) or its path fallback.
Source tokens request contents, pull_requests and metadata read only; checks and
webhooks must stay disabled. Evidence holds no token, key, source text or body.
The public report omits operator IDs; ``--private-out`` receives repo/installation IDs.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofhunter.config import ServiceRole, Settings, load_enrollments, load_settings
from proofhunter.contracts import Enrollment, canonical_sha256, enrollment_fingerprint
from proofhunter.github.client import CallRecord
from proofhunter.github.source import GithubSource
from proofhunter.github.validation import GithubRepositoryValidator
from proofhunter.ports import AdapterError, ConfigError

MAX_CASES = 4
MAX_CALLS_PER_CASE = 7  # pull + (commit, tree, contents) per side
TOKEN_CALLS = 1  # one cached installation token for the single enrolled repository
DEADLINE_SECONDS = 120
SOURCE_PERMISSIONS = {"contents": "read", "pull_requests": "read", "metadata": "read"}


@dataclass(frozen=True)
class Case:
    pr_url: str
    base_sha: str
    head_sha: str


class CallBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls: list[CallRecord] = []

    def __call__(self, record: CallRecord) -> None:
        self.calls.append(record)

    def check(self) -> None:
        if len(self.calls) > self.limit:
            raise RuntimeError("GitHub smoke call budget exceeded")


def _call(record: CallRecord) -> dict[str, Any]:
    return {
        "method": record.method,
        "route": record.route,
        "status": record.status,
        "latency_ms": record.latency_ms,
        "request_id": record.request_id,
        "utc": record.utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def config_fingerprint(settings: Settings, enrollments: Sequence[Enrollment]) -> str:
    """Non-secret smoke configuration digest (no App ID, installation ID or key source)."""
    return canonical_sha256(
        {
            "api_origin": settings.github.api_origin,
            "api_version": settings.github.api_version,
            "source_permissions": SOURCE_PERMISSIONS,
            "checks": settings.github.enable_checks,
            "webhook": settings.github.enable_webhook,
            "enrollments": sorted(enrollment_fingerprint(e) for e in enrollments),
        }
    )


async def run_cases(
    source: GithubSource,
    validator: GithubRepositoryValidator,
    cases: Sequence[Case],
    budget: CallBudget,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    public: list[dict[str, Any]] = []
    private: list[dict[str, Any]] = []
    for case in cases:
        start = len(budget.calls)
        target, enrollment = validator.resolve(case.pr_url)
        entry: dict[str, Any] = {"pr_url": target.canonical_url, "number": target.number}
        try:
            revision = await source.pin(target)
            budget.check()
            entry.update(
                base_sha=revision.base_sha,
                head_sha=revision.head_sha,
                base_matches_expected=revision.base_sha == case.base_sha,
                head_matches_expected=revision.head_sha == case.head_sha,
            )
            if not (entry["base_matches_expected"] and entry["head_matches_expected"]):
                raise RuntimeError("pinned revision differs from expected original SHAs")
            bundle = await source.fetch(revision, enrollment)
            budget.check()
            entry["files"] = {
                f.side: {
                    "commit_sha": f.commit_sha,
                    "path": f.path,
                    "blob_sha": f.blob_sha,
                    "content_sha256": f.content_sha256,
                    "size_bytes": f.size_bytes,
                    "tree_entry": "100644 blob (verified)",
                }
                for f in (bundle.base, bundle.head)
            }
            entry["result"] = "pass"
        except AdapterError as error:
            entry.update(result="fail", reason_code=error.code.value)
        except RuntimeError as error:
            entry.update(result="fail", reason_code=str(error))
        entry["calls"] = [_call(c) for c in budget.calls[start:]]
        public.append(entry)
        private.append(
            {
                "pr_url": target.canonical_url,
                "repo_full_name": enrollment.repo_full_name,
                "repo_id": target.repo_id,
                "installation_id": target.installation_id,
                "enrollment_fingerprint": enrollment_fingerprint(enrollment),
            }
        )
        if entry["result"] != "pass":
            break
    return public, private


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m proofhunter.github.smoke")
    parser.add_argument(
        "--case", action="append", nargs=3, metavar=("PR_URL", "BASE_SHA", "HEAD_SHA"),
        required=True,
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--private-out", type=Path, required=True)
    return parser.parse_args(argv)


async def main_async(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    args = _parse(argv)
    cases = [Case(*c) for c in args.case]
    if not 1 <= len(cases) <= MAX_CASES:
        raise ConfigError(f"between 1 and {MAX_CASES} cases are allowed")
    if environ.get("PROOFHUNTER_PROFILE") != "live":
        raise ConfigError("GitHub smoke requires PROOFHUNTER_PROFILE=live")
    settings = load_settings(environ, role=ServiceRole.WORKER)
    if settings.github.enable_checks or settings.github.enable_webhook:
        raise ConfigError("GitHub smoke requires checks and webhooks disabled")
    enrollments = load_enrollments(settings.enrollments_path)
    validator = GithubRepositoryValidator(enrollments)
    budget = CallBudget(TOKEN_CALLS + MAX_CALLS_PER_CASE * len(cases))
    from proofhunter.github import create_source_port

    source = create_source_port(settings, enrollments, observer=budget)
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            public, private = await run_cases(source, validator, cases, budget)
    finally:
        await source.aclose()
    passed = len(public) == len(cases) and all(e["result"] == "pass" for e in public)
    report = {
        "kind": "proofhunter-github-source-smoke-v1",
        "execution_mode": "live",
        "api_version": settings.github.api_version,
        "source_permissions": SOURCE_PERMISSIONS,
        "checks_enabled": False,
        "webhook_enabled": False,
        "config_fingerprint": config_fingerprint(settings, enrollments),
        "total_calls": len(budget.calls),
        "call_budget": budget.limit,
        "result": "pass" if passed else "fail",
        "cases": public,
    }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.private_out.write_text(json.dumps({"cases": private}, indent=2, sort_keys=True) + "\n")
    print(f"github source smoke: {report['result']} ({len(budget.calls)} calls)")
    return 0 if passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(sys.argv[1:] if argv is None else argv, os.environ))
    except ConfigError as error:
        print(f"github source smoke: config error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
