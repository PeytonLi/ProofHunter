"""The gate scaffold is real: it fails when required modules are missing."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from proofhunter import ports

REPO = Path(__file__).resolve().parents[2]


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ph_gate", REPO / "tools" / "gate.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_required_module_check_fails_on_missing_modules() -> None:
    gate = _gate()
    missing = [m for m in ports.REQUIRED_IMPLEMENTATIONS
               if importlib.util.find_spec(m.split(".")[0]) is None
               or (m.startswith("proofhunter.") and not (REPO / "src" / Path(
                   *m.split("."))).with_suffix(".py").exists() and not (
                   REPO / "src" / Path(*m.split("."))).is_dir())]
    result = subprocess.run([sys.executable, "-c", gate.CHECK_MODULES], cwd=REPO,
                            capture_output=True, text=True, check=False)
    if missing:
        assert result.returncode == 1
        assert "missing required implementations" in result.stderr


def test_offline_env_strips_provider_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gate = _gate()
    for name in ("MODEL_API_KEY", "SUBQ_API_KEY", "DAYTONA_API_KEY", "GITHUB_WEBHOOK_SECRET",
                 "AGENT_SEED_PHRASE", "PROOFHUNTER_API_TOKEN", "AGENTVERSE_API_KEY"):
        monkeypatch.setenv(name, "fixture-value")
    env = gate.offline_env()
    assert env["PROOFHUNTER_PROFILE"] == "offline"
    assert not {"MODEL_API_KEY", "SUBQ_API_KEY", "DAYTONA_API_KEY", "GITHUB_WEBHOOK_SECRET",
                "AGENT_SEED_PHRASE", "PROOFHUNTER_API_TOKEN", "AGENTVERSE_API_KEY"} & set(env)


def test_gate_covers_section_10_checks() -> None:
    source = (REPO / "tools" / "gate.py").read_text()
    for step in ("required modules", "full offline suite", "demo", "counterexample replay",
                 "tampered receipt is refused", "sample bench", "lock is current",
                 "offline contract tests"):
        assert step in source


def test_ownership_map_covers_all_roles() -> None:
    assert set(ports.MODULE_OWNERS) == {"github", "meta", "snap", "bayes", "runner", "oracle",
                                        "acp", "store", "bench", "integration"}


def test_setup_gate_propagates_a_failed_contract_check(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    gate = _gate()
    labels: list[str] = []

    def fail_contracts(label: str, args: list[str]) -> None:
        labels.append(label)
        if label == "offline contract tests":
            assert args == ["uv", "run", "--locked", "pytest", "-q", "tests/contracts"]
            raise gate.GateFailure("injected nonzero contract-test exit")

    monkeypatch.setattr(gate, "run", fail_contracts)
    assert gate.main(["setup"]) == 1
    assert labels[-1] == "offline contract tests"
    assert "setup FAILED" in capsys.readouterr().err


def test_gate_run_rejects_actual_nonzero_exit() -> None:
    gate = _gate()
    with pytest.raises(gate.GateFailure, match="exit 9, expected zero"):
        gate.run("negative subprocess", [sys.executable, "-c", "raise SystemExit(9)"])
