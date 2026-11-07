"""Launch the ProofHunter PRD roles in isolated Git worktrees."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys


PARALLEL = ("github", "meta", "snap", "bayes", "runner", "oracle", "acp", "store", "bench")
ROLES = ("setup", *PARALLEL, "integration", "verification")
SCOPES = {
    role: (f"src/proofhunter/{role}/", f"tests/{role}/", f"docs/agent-handoffs/{role}.md")
    for role in PARALLEL
}
SCOPES.update(
    setup=(
        "pyproject.toml", "uv.lock", ".python-version", ".env.example", ".gitignore",
        "README.md", "config/", "schemas/", "fixtures/", "tests/contracts/",
        "src/proofhunter/__init__.py", "src/proofhunter/contracts.py",
        "src/proofhunter/ports.py", "src/proofhunter/config.py", "tools/gate.py",
        ".github/workflows/", "docs/setup.md", "docs/provider-smoke.md",
    ),
    integration=(
        "src/proofhunter/pipeline.py", "src/proofhunter/worker.py", "src/proofhunter/cli.py",
        "src/proofhunter/__main__.py", "tests/integration/", "tools/gate.py",
        "README.md", "docs/integration.md", "ops/deploy/",
    ),
    verification=(),
)
SCOPES["runner"] += ("ops/sandbox/",)
SCOPES["store"] += ("src/proofhunter/api.py",)
SCOPES["bench"] = ("bench/", "tests/bench/", "docs/agent-handoffs/bench.md")


def run(command, cwd, *, stdin=None, log=None, env=None, timeout=900):
    options = dict(cwd=cwd, input=stdin, encoding="utf-8", errors="replace", env=env,
                   timeout=timeout, check=True)
    if log is None:
        return subprocess.run(command, capture_output=True, **options).stdout
    with log.open("w", encoding="utf-8") as stream:
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, **options)


def git(cwd, *args):
    return run(["git", *args], cwd)


def clean(cwd):
    if git(cwd, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError(f"Expected clean worktree: {cwd}")


def prompt_blocks(document):
    blocks = re.findall(r"^<!-- prompt:([a-z]+) -->\n```text\n(.*?)\n```$",
                        document, re.MULTILINE | re.DOTALL)
    if tuple(role for role, _ in blocks) != ROLES:
        raise RuntimeError("PRD must contain one prompt for each role in plan order")
    return dict(blocks)


def inside_scope(path, scope):
    return any(path.startswith(item) if item.endswith("/") else path == item for item in scope)


def offline_environment():
    prefixes = ("GITHUB_", "GH_", "DAYTONA_", "SUBQ_", "META_", "AGENTVERSE_",
                "ASI_", "ILABS_", "PROOFHUNTER_")
    exact = {"MODEL_API_KEY", "AGENT_SEED_PHRASE", "UV_OFFLINE", "UV_PROJECT_ENVIRONMENT",
             "VIRTUAL_ENV"}
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in exact and not key.upper().startswith(prefixes)}
    env["PROOFHUNTER_PROFILE"] = "offline"
    return env


def coordinate(repo, prompts, jobs):
    codex = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if not codex:
        raise RuntimeError("Put the Codex executable on PATH and sign in first")
    clean(repo)
    git(repo, "ls-files", "--error-unmatch", "ProofHunter-PRD.md")
    git(repo, "var", "GIT_AUTHOR_IDENT")
    git(repo, "var", "GIT_COMMITTER_IDENT")
    initial = git(repo, "rev-parse", "HEAD").strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}-{secrets.token_hex(3)}"
    state = repo.parent / f"{repo.name}-coord-{run_id}"
    state.mkdir()
    prefix = f"proofhunter/{run_id}"
    cache = state / "uv-cache"
    cache.mkdir()
    setup_env = offline_environment()
    setup_env["UV_CACHE_DIR"] = str(cache)
    env = {**setup_env, "UV_OFFLINE": "1"}
    print(f"Worktrees and logs: {state}", flush=True)

    def add_tree(role, base):
        tree = state / role
        git(repo, "worktree", "add", "-b", f"{prefix}/{role}", str(tree), base)
        return tree

    def agent(role, tree, base):
        print(f"Starting {role} at {base[:12]}", flush=True)
        report = state / f"{role}.report.md"
        scope = ", ".join(SCOPES[role]) or "no changes"
        prompt = (
            f"{prompts[role]}\n\nAutomation envelope for this session:\n"
            f"Base SHA: {base}. Permitted paths: {scope}.\n"
            "Work offline for ProofHunter and use the local fixture profile. "
            "Leave changes unstaged and uncommitted, the coordinator handles Git commits. "
            "Keep the required verdict on the first line of your final response."
        )
        log = state / f"{role}.log"
        network = "true" if role == "setup" else "false"
        run([codex, "-a", "never", "exec", "--ephemeral", "-C", str(tree),
             "-s", "workspace-write", "--add-dir", str(cache), "-c",
             f"sandbox_workspace_write.network_access={network}", "-o", str(report), "-"],
            tree, stdin=prompt, log=log, env=setup_env if role == "setup" else env, timeout=2400)
        lines = report.read_text(encoding="utf-8").strip().splitlines()
        expected = "VERDICT: PASS_OFFLINE" if role == "verification" else "VERDICT: PASS"
        if not lines or lines[0].strip() != expected:
            raise RuntimeError(f"{role} did not pass. See {report}")
        print(f"Finished {role}", flush=True)

    def accept(role, tree, base):
        if (git(tree, "rev-parse", "HEAD").strip() != base or
                git(tree, "branch", "--show-current").strip() != f"{prefix}/{role}"):
            raise RuntimeError(f"{role} moved its assigned branch or HEAD")
        if role == "verification":
            clean(tree)
            return base
        git(tree, "add", "-A", "--", ".")
        paths = git(tree, "diff", "--cached", "--name-only", "--no-renames", "-z").split("\0")
        changes = [path for path in paths if path]
        if not changes or any(not inside_scope(path, SCOPES[role]) for path in changes):
            raise RuntimeError(f"{role} has missing or out-of-scope changes: {changes}")
        if role in PARALLEL and f"docs/agent-handoffs/{role}.md" not in changes:
            raise RuntimeError(f"{role} has no handoff")
        git(tree, "diff", "--cached", "--check")
        git(tree, "commit", "-m", f"ProofHunter: {role}")
        clean(tree)
        return git(tree, "rev-parse", "HEAD").strip()

    def gate(tree, stage):
        run([sys.executable, "tools/gate.py", stage], tree,
            log=state / f"gate-{stage}.log", env=env)
        clean(tree)

    setup = add_tree("setup", initial)
    agent("setup", setup, initial)
    frozen = accept("setup", setup, initial)
    gate(setup, "setup")

    trees = {role: add_tree(role, frozen) for role in PARALLEL}
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        sessions = {role: executor.submit(agent, role, tree, frozen) for role, tree in trees.items()}
        for role in PARALLEL:
            sessions[role].result()
    commits = {role: accept(role, tree, frozen) for role, tree in trees.items()}

    integration = add_tree("integration", frozen)
    for role, commit in commits.items():
        git(integration, "merge", "--no-ff", "-m", f"Integrate {role}", commit)
    joined = git(integration, "rev-parse", "HEAD").strip()
    agent("integration", integration, joined)
    candidate = accept("integration", integration, joined)
    gate(integration, "integrated")

    verification = add_tree("verification", candidate)
    agent("verification", verification, candidate)
    accept("verification", verification, candidate)
    result = {"verdict": "PASS_OFFLINE", "candidate_sha": candidate,
              "candidate_branch": f"{prefix}/integration", "live_release": "pending"}
    (state / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--jobs", type=int, default=len(PARALLEL))
    parser.add_argument("--dry-run", action="store_true", help="Read the PRD and print the plan only")
    args = parser.parse_args()
    if not 1 <= args.jobs <= len(PARALLEL):
        parser.error(f"--jobs must be between 1 and {len(PARALLEL)}")
    try:
        repo = args.repo.resolve()
        prompts = prompt_blocks((repo / "ProofHunter-PRD.md").read_text(encoding="utf-8-sig"))
        if args.dry_run:
            print(f"setup -> G0 -> [{', '.join(PARALLEL)}] (max {args.jobs} concurrent) "
                  "-> join -> integration -> G2 -> verification -> G3 offline")
            return 0
        if Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve() != repo:
            raise RuntimeError("--repo must point to the Git repository root")
        coordinate(repo, prompts, args.jobs)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
