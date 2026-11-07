"""Offline benchmark entrypoint with frozen policies and separate scoring."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from bench.arms import ARMS, ArmInput, run_arm
from bench.corpus import ROOT, Manifest, load, sources
from bench.freeze import POLICY_PATH, load_policy
from bench.harness import BenchRunner
from bench.holdout import validate
from bench.scoring import Row, score, write_outputs
from fixtures import catalog
from fixtures.adapters import Fault, FaultKey, FixtureMeta, planning_context
from proofhunter import bayes
from proofhunter.contracts import IsolationStatus, ObservationOutcome, Side, canonical_sha256


def seeds(seed: int) -> list[int]:
    return [int(canonical_sha256({"seed": seed, "replicate": i})[:8], 16) % (2**31)
            for i in range(5)]


async def prepare(manifest: Manifest, root: Path, case_id: str, bank_seed: int) -> ArmInput:
    case = next(c for c in manifest.cases if c.case_id == case_id)
    base, head = sources(case, root)
    context = planning_context("00000000-0000-4000-8000-000000000001", base, head)
    plan = await FixtureMeta().propose(context)
    return ArmInput(base, head, catalog.demo_enrollment(), plan,
                    bayes.generate(plan, bank_seed))


def enrolled(manifest: Manifest, root: Path) -> dict[str, bytes]:
    return {getattr(c, f"{side}_sha256"): (root / getattr(c, f"{side}_path")).read_bytes()
            for c in manifest.cases for side in ("base", "head")}


async def adversarial(seed: int) -> list[Row]:
    manifest = load()
    inputs = await prepare(manifest, ROOT, "per_line", seed)
    allowed = enrolled(manifest, ROOT)
    scenarios: list[tuple[str, dict[FaultKey, Fault], dict[str, bytes], IsolationStatus]] = [
        ("timeout_head", {(Side.HEAD, None): ObservationOutcome.TIMEOUT}, allowed,
         IsolationStatus.OFFLINE_FIXTURE),
        ("invalid_output", {(Side.HEAD, None): ObservationOutcome.INVALID}, allowed,
         IsolationStatus.OFFLINE_FIXTURE),
        ("flaky_confirmation", {(Side.CONFIRM, None): 999999}, allowed,
         IsolationStatus.OFFLINE_FIXTURE),
        ("isolation_unverified", {}, allowed, IsolationStatus.UNVERIFIED),
        ("unknown_source", {}, {}, IsolationStatus.OFFLINE_FIXTURE),
    ]
    rows = []
    for name, faults, allowlist, isolation in scenarios:
        runner = BenchRunner(allowlist, inputs.enrollment, faults=faults, isolation=isolation)
        row = await run_arm(inputs, "eig_deterministic", seed, runner)
        row["scenario"] = name
        rows.append(row)
    return rows


async def execute(split: str, seed: int, root: Path,
                  policy_path: Path = POLICY_PATH) -> tuple[list[Row], list[Row]]:
    policy = load_policy(policy_path)
    if split == "holdout" and policy_path.resolve().is_relative_to(ROOT.parent.parent.resolve()):
        raise ValueError("holdout requires an independently reviewed external policy")
    manifest = validate(root) if split == "holdout" else load(root)
    if split != "holdout" and root.resolve() != ROOT.resolve():
        raise ValueError("external corpus only allowed with --split holdout")
    selected = [c for c in manifest.cases
                if (c.case_id in manifest.sample if split == "sample" else c.split == split)]
    if not selected:
        raise ValueError("empty split")
    preregistered = seeds(seed)
    allowlist = enrolled(manifest, root)
    if (manifest.manifest_sha256 != policy.manifest_sha256 or seed != policy.master_seed
            or preregistered != policy.replicate_seeds
            or policy.max_pairs != 6 or policy.max_confirmations != 1
            or set(allowlist) != set(policy.reviewed_source_sha256)):
        raise ValueError("manifest/seeds/budgets/source review differ from frozen policy")
    rows: list[Row] = []
    for case in selected:
        inputs = await prepare(manifest, root, case.case_id, seed)
        for replicate_seed in preregistered:
            for arm in ARMS:
                runner = BenchRunner(allowlist, inputs.enrollment,
                                     reviewed_source_sha256=frozenset(policy.reviewed_source_sha256))
                row = await run_arm(inputs, arm, replicate_seed, runner)
                row.update({"manifest_sha256": manifest.manifest_sha256,
                            "benchmark_policy_sha256": policy.policy_sha256,
                            "implementation_sha256": policy.source_sha256,
                            "corpus_version": manifest.version, "split": split,
                            "preregistered_seeds": preregistered,
                            "source_sha256": {"base": inputs.base.content_sha256,
                                              "head": inputs.head.content_sha256},
                            "meta_plan_sha256": canonical_sha256(inputs.plan.model_dump()),
                            "meta_mode": "recorded/offline_fixture",
                            "meta_live_calls": 0})
                rows.append(score(row, case))
    return rows, await adversarial(seed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", required=True, action="store_true")
    parser.add_argument("--split", choices=("sample", "dev", "holdout"), default="sample")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus-dir", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    args = parser.parse_args(argv)
    if args.split == "holdout" and args.corpus_dir.resolve() == ROOT.resolve():
        parser.error("holdout requires an external --corpus-dir")
    if not args.output.parent.is_dir():
        parser.error("output parent directory does not exist")
    try:
        rows, faults = asyncio.run(execute(args.split, args.seed, args.corpus_dir, args.policy))
        print(write_outputs(args.output, rows, faults))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
