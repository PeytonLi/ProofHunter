"""ProofHunter build gates (PRD section 10). Standard library only.

    python tools/gate.py setup        # G0: locked offline contract checks
    python tools/gate.py integrated   # G2: full offline suite, demos, replay, tamper, bench

Every step runs through ``uv run --locked`` with ``PROOFHUNTER_PROFILE=offline``
and provider credentials stripped from the environment. Any failure exits 1.
Outputs of ``integrated`` go to a temporary directory; the worktree stays clean.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVIDENCE: Path | None = None

SETUP_PATHS = (
    "src/proofhunter/__init__.py", "src/proofhunter/contracts.py", "src/proofhunter/ports.py",
    "src/proofhunter/config.py", "fixtures", "tests/contracts", "tools/gate.py",
)
TYPED_PATHS = (
    "src/proofhunter/__init__.py", "src/proofhunter/contracts.py", "src/proofhunter/ports.py",
    "src/proofhunter/config.py",
)
_STRIP_PREFIXES = ("GITHUB_", "GH_", "DAYTONA_", "SUBQ_", "META_", "AGENTVERSE_", "ASI_",
                   "ILABS_", "PROOFHUNTER_")
_STRIP_EXACT = {"MODEL_API_KEY", "AGENT_SEED_PHRASE", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}

CHECK_MODULES = r"""
import importlib, sys
from proofhunter.ports import REQUIRED_IMPLEMENTATIONS
missing = []
for module, names in REQUIRED_IMPLEMENTATIONS.items():
    try:
        loaded = importlib.import_module(module)
    except Exception as error:
        missing.append(f"{module} (import failed: {type(error).__name__}: {error})")
        continue
    for name in names:
        if not callable(getattr(loaded, name, None)):
            missing.append(f"{module}.{name}")
if missing:
    print("missing required implementations:", *missing, sep="\n  ", file=sys.stderr)
    sys.exit(1)
print(f"required implementations present ({len(REQUIRED_IMPLEMENTATIONS)} modules)")
"""


class GateFailure(Exception):
    pass


def offline_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in _STRIP_EXACT and not k.upper().startswith(_STRIP_PREFIXES)}
    env["PROOFHUNTER_PROFILE"] = "offline"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.update(extra or {})
    return env


def run(label: str, args: Sequence[str], *, env: dict[str, str] | None = None,
        expect_failure: bool = False, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv")
    if uv is None:
        raise GateFailure("uv is not on PATH")
    command = [uv if a == "uv" else a for a in args]
    shown = " ".join(a if len(a) <= 80 else "<script>" for a in args)
    print(f"[gate] {label}: {shown}", flush=True)
    result = subprocess.run(command, cwd=cwd, env=env or offline_env(), text=True,
                            capture_output=True, check=False)
    output = (result.stdout + result.stderr).strip()
    if EVIDENCE is not None:
        name = "".join(character if character.isalnum() else "-" for character in label)
        (EVIDENCE / f"{name}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if output:
        print("\n".join("    " + line for line in output.splitlines()[-40:]), flush=True)
    failed = result.returncode != 0
    if failed != expect_failure:
        expectation = "nonzero" if expect_failure else "zero"
        raise GateFailure(f"{label}: exit {result.returncode}, expected {expectation}")
    print(f"[gate] {label}: ok", flush=True)
    return result


def uv_run(*args: str) -> list[str]:
    return ["uv", "run", "--locked", *args]


def setup_gate() -> None:
    run("lock is current", ["uv", "lock", "--check"])
    run("environment matches lock", ["uv", "sync", "--locked", "--check"])
    run("fixture and schema snapshots", uv_run("python", "-m", "fixtures.build", "--check"))
    run("offline contract tests", uv_run("pytest", "-q", "tests/contracts"))
    run("lint (setup paths)", uv_run("ruff", "check", *SETUP_PATHS))
    run("types (shared modules)", uv_run("mypy", *TYPED_PATHS, "fixtures"))


def _load(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GateFailure(f"{path.name} is not a JSON object")
    return data


def _demo_checks(buggy: dict[str, object], fixed: dict[str, object]) -> None:
    for name, receipt in (("buggy", buggy), ("fixed", fixed)):
        if receipt.get("execution_mode") != "offline_fixture":
            raise GateFailure(f"{name} receipt is not labeled offline_fixture")
        if not receipt.get("receipt_sha256"):
            raise GateFailure(f"{name} receipt is not sealed")
    if buggy.get("verdict") != "confirmed_regression":
        raise GateFailure(f"buggy verdict {buggy.get('verdict')!r}")
    probes = buggy.get("probes")
    number = buggy.get("counterexample_probe_no")
    if not isinstance(probes, list) or not isinstance(number, int):
        raise GateFailure("buggy receipt has no counterexample probe")
    probe = probes[number - 1]
    observed = (
        probe["oracle"]["expected_cents"],
        probe["base"]["value"],
        probe["head"]["value"],
        (probe.get("confirmation") or {}).get("value"),
    )
    if (
        probe["candidate"]["prices_cents"] != [15, 15, 15]
        or probe["candidate"]["discount_percent"] != 10
    ):
        raise GateFailure("buggy seed-17 counterexample is not the pinned sentinel")
    if observed[0] != observed[1] or observed[2] != observed[3] or observed[1] == observed[2]:
        raise GateFailure(f"buggy evidence chain invalid: {observed}")
    if observed != (41, 41, 42, 42):
        raise GateFailure(f"buggy sentinel evidence {observed}, expected (41, 41, 42, 42)")
    if fixed.get("verdict") != "no_defect_found":
        raise GateFailure(f"fixed verdict {fixed.get('verdict')!r}")
    coverage = fixed.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("missing"):
        raise GateFailure("fixed receipt is missing required coverage")
    print("[gate] demo evidence: ok", flush=True)


def integrated_gate() -> None:
    run("required modules", uv_run("python", "-c", CHECK_MODULES))
    setup_gate()
    run("full offline suite", uv_run("pytest", "-q"))
    run("lint (all paths)", uv_run("ruff", "check", "."))
    run("types (production, bench, tools)", uv_run("mypy", "src", "bench", "tools"))
    run("types (integration tests)", uv_run("mypy", "tests/integration"))
    with (
        nullcontext(str(EVIDENCE))
        if EVIDENCE is not None
        else tempfile.TemporaryDirectory(prefix="proofhunter-g2-")
    ) as tmp:
        work = Path(tmp)
        env = offline_env({"PROOFHUNTER_DB_PATH": str(work / "proofhunter.sqlite3")})
        buggy, fixed = work / "buggy-receipt.json", work / "fixed-receipt.json"
        for case, path in (("buggy", buggy), ("fixed", fixed)):
            run(
                f"demo {case}",
                uv_run(
                    "python",
                    "-m",
                    "proofhunter",
                    "demo",
                    "--offline",
                    "--case",
                    case,
                    "--seed",
                    "17",
                    "--receipt",
                    str(path),
                ),
                env=env,
            )
        _demo_checks(_load(buggy), _load(fixed))
        run(
            "counterexample replay",
            uv_run(
                "python",
                "-m",
                "proofhunter",
                "replay",
                "--offline",
                "--receipt",
                str(buggy),
                "--probe",
                "counterexample",
            ),
            env=env,
        )
        tampered = work / "tampered-receipt.json"
        data = _load(buggy)
        data["seed"] = int(str(data["seed"])) + 1
        tampered.write_text(json.dumps(data), encoding="utf-8")
        run(
            "tampered receipt is refused",
            uv_run(
                "python",
                "-m",
                "proofhunter",
                "replay",
                "--offline",
                "--receipt",
                str(tampered),
                "--probe",
                "counterexample",
            ),
            env=env,
            expect_failure=True,
        )
        results = work / "bench-results.jsonl"
        run(
            "sample bench",
            uv_run(
                "python",
                "-m",
                "bench.run",
                "--offline",
                "--split",
                "sample",
                "--seed",
                "17",
                "--output",
                str(results),
            ),
            env=env,
        )
        rows = [line for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            raise GateFailure("sample bench wrote no rows")
        for line in rows:
            json.loads(line)
        print(f"[gate] sample bench rows: {len(rows)}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    global EVIDENCE
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=("setup", "integrated"))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.evidence is not None:
        args.evidence.mkdir(parents=True, exist_ok=True)
        EVIDENCE = args.evidence.resolve()
    try:
        setup_gate() if args.stage == "setup" else integrated_gate()
    except GateFailure as error:
        print(f"[gate] {args.stage} FAILED: {error}", file=sys.stderr, flush=True)
        return 1
    print(f"[gate] {args.stage} PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
