"""Track B: metadata-only SWE-smith manifests. Never imports, installs or executes dataset,
repository, test or patch code; patches are only scanned as text for file paths.

Inputs (``source`` directory):
  ``metadata.jsonl``        one row per dataset row (export of the pinned Parquet shards,
                            see ``export_metadata``), fields instance_id, repo, image_name,
                            FAIL_TO_PASS, PASS_TO_PASS, patch_files, patch_sha256
  ``shards.json``           {"revision": ..., "files": {name: sha256}}
  ``pins.json``             assessment pins (repo upstream, SPDX license, mirror main sha)
  ``split_proposal.json``   repository-disjoint split proposal (instance ids per repo)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

from research.schemas import models as M

DATASET_HF_ID: Final = "SWE-bench/SWE-smith-py"
DATASET_REVISION: Final = "77cab9055d42ab4a5c25c89a8f937096db13558e"
MAX_F2P: Final = 20
MAX_TESTS: Final = 200
TEST_ID: Final = re.compile(r"^[A-Za-z0-9_./\[\]:,=+-]{1,300}$")
NODE_ID: Final = re.compile(r"^[A-Za-z0-9_./-]+\.py::[A-Za-z0-9_.:\[\],=+-]+$")
PERMISSIVE: Final = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "0BSD",
                     "PSF-2.0", "Unlicense"}
COPYLEFT: Final = {"GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0", "MPL-2.0"}


def export_metadata(parquet_dir: Path, out: Path) -> int:
    """Export metadata columns from the pinned Parquet shards (data only; needs pyarrow).

    Run outside the locked research env, e.g.
    ``uv run --no-project --with pyarrow==18.1.0 --with pydantic python -m research.data.swe_smith
    export <parquet_dir> <out.jsonl>``. The patch is reduced to its file paths and a hash.
    """
    import pyarrow.parquet as pq

    rows = 0
    with out.open("w") as fh:
        for shard in sorted(parquet_dir.glob("*.parquet")):
            table = pq.read_table(shard, columns=["instance_id", "repo", "image_name",
                                                  "FAIL_TO_PASS", "PASS_TO_PASS", "patch"])
            for row in table.to_pylist():
                patch = row.pop("patch") or ""
                row["patch_files"] = sorted(set(re.findall(r"^diff --git a/(\S+) b/", patch,
                                                           flags=re.M)))
                row["patch_sha256"] = hashlib.sha256(_normalize_patch(patch).encode()).hexdigest()
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                rows += 1
    return rows


def _normalize_patch(patch: str) -> str:
    lines = [ln for ln in patch.splitlines() if not ln.startswith(("index ", "@@"))]
    return "\n".join(ln.rstrip() for ln in lines)


def strategy_of(instance_id: str) -> str:
    tail = instance_id.split(".")[-1]
    return tail.rsplit("__", 1)[0] if "__" in tail else tail


def redistribution(spdx: str) -> str:
    return "allowed" if spdx in PERMISSIVE else "restricted" if spdx in COPYLEFT else "unknown"


def reject_reason(row: Mapping[str, object], spdx: str | None,
                  seen_patches: set[str]) -> str | None:
    f2p = list(row["FAIL_TO_PASS"])  # type: ignore[call-overload]
    if len(f2p) > MAX_F2P:
        return "f2p_gt_20"
    if not f2p or not all(NODE_ID.match(t) and TEST_ID.match(t) for t in f2p):
        return "f2p_not_allowlistable_pytest_nodeid"
    files = list(row["patch_files"])  # type: ignore[call-overload]
    if any(not f.endswith(".py") for f in files):
        return "non_py_file_in_patch"
    if any("/tests/" in f"/{f}" or Path(f).name.startswith("test_") or
           Path(f).name == "conftest.py" for f in files):
        return "patch_touches_tests"
    if strategy_of(str(row["instance_id"])) in ("lm_rewrite", "lm_modify"):
        return "lm_rewrite_label_noise"
    if row["patch_sha256"] in seen_patches:
        return "duplicate_patch"
    if spdx in (None, "NONE"):
        return "upstream_no_license"
    return None


def _read_jsonl(path: Path) -> Iterable[dict[str, object]]:
    with path.open() as fh:
        for line in fh:
            yield json.loads(line)


def prepare_swe_smith_manifest(source: Path, out: Path) -> Sequence[M.SweSmithTask]:
    pins = json.loads((source / "pins.json").read_text())
    proposal = json.loads((source / "split_proposal.json").read_text())
    shards = json.loads((source / "shards.json").read_text())
    if shards["revision"] != DATASET_REVISION:
        raise ValueError("dataset revision is not the pinned revision")
    files_sha = M.canonical_sha256(shards["files"])
    repos = pins["swe_smith_py_repos"]
    split_of: dict[str, tuple[M.Split, str]] = {}
    for split, by_repo in proposal["splits"].items():
        for repo, ids in by_repo.items():
            for iid in ids:
                split_of[iid] = (M.Split(split), repo)
    seen: set[str] = set()
    reasons: Counter[str] = Counter()
    survivors: dict[str, int] = defaultdict(int)
    tasks: list[M.SweSmithTask] = []
    dropped_p2p = 0
    rows = 0
    for row in sorted(_read_jsonl(source / "metadata.jsonl"), key=lambda r: str(r["instance_id"])):
        rows += 1
        pin = repos.get(str(row["repo"]))
        spdx = pin.get("upstream_license_spdx_github") if pin else None
        reason = reject_reason(row, spdx, seen)
        seen.add(str(row["patch_sha256"]))
        if reason:
            reasons[reason] += 1
            continue
        survivors[str(row["repo"])] += 1
        iid = str(row["instance_id"])
        if iid not in split_of:
            reasons["survivor_not_selected_by_split_cap"] += 1
            continue
        split, group = split_of[iid]
        f2p = [M.TestCandidate(test_id=t, origin="fail_to_pass") for t in row["FAIL_TO_PASS"]]
        p2p_ids = sorted(t for t in row["PASS_TO_PASS"] if TEST_ID.match(t)
                         and t not in row["FAIL_TO_PASS"])
        dropped_p2p += len(row["PASS_TO_PASS"]) - len(p2p_ids)  # type: ignore[arg-type]
        room = MAX_TESTS - len(f2p)
        p2p = [M.TestCandidate(test_id=t, origin="pass_to_pass") for t in p2p_ids[:room]]
        tasks.append(M.SweSmithTask(
            schema_version="research-swe-smith-task-v1", track=M.Track.B, instance_id=iid,
            dataset_id=DATASET_HF_ID, dataset_revision=DATASET_REVISION,
            dataset_file_sha256=files_sha, repo=str(row["repo"]),
            repo_commit=pin["mirror_main_sha"], license_spdx=spdx,
            license_verified=spdx in PERMISSIVE | COPYLEFT, redistribution=redistribution(spdx),
            defect_origin=f"{strategy_of(iid)}:{row['repo']}", split=split, split_group=group,
            candidate_tests=f2p + p2p, label_status="unverified_design_only",
            execution_status="blocked_no_qualified_sandbox", executed=False))
    missing = sorted(set(split_of) - {t.instance_id for t in tasks})
    out.mkdir(parents=True, exist_ok=True)
    groups: dict[str, set[str]] = defaultdict(set)
    for t in tasks:
        groups[t.split_group].add(t.split.value)
    for split in M.Split:
        rows_out = [t.model_dump(mode="json") for t in tasks if t.split is split]
        (out / f"swe-smith-{split.value}.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows_out))
    report = {
        "schema": "track-b-exclusion-report-v1",
        "dataset": f"{DATASET_HF_ID}@{DATASET_REVISION}",
        "dataset_file_sha256": files_sha,
        "shards": shards["files"],
        "rows": rows,
        "rejections": dict(sorted(reasons.items())),
        "proposal_rejections": proposal.get("rejections"),
        "metadata_survivors": sum(survivors.values()),
        "survivor_repos": len(survivors),
        "tasks": {s.value: sum(t.split is s for t in tasks) for s in M.Split},
        "repos": {s.value: len({t.split_group for t in tasks if t.split is s}) for s in M.Split},
        "proposal_ids_not_reproduced": len(missing),
        "proposal_ids_not_reproduced_examples": missing[:10],
        "repos_in_multiple_splits": sorted(g for g, s in groups.items() if len(s) > 1),
        "pass_to_pass_ids_dropped_by_pattern": dropped_p2p,
        "candidate_tests_cap": MAX_TESTS,
        "trusted_tests": ("candidate test ids come from FAIL_TO_PASS/PASS_TO_PASS and must be run "
                          "from the clean mirror commit (repo_commit = mirror main sha); defective "
                          "instance branches delete the failing tests"),
        "repo_commit_source": "pins.json swe_smith_py_repos[*].mirror_main_sha",
        "executed": False,
        "schema_gaps": [
            "license_verified=True means a GitHub SPDX id was recorded by the assessment "
            "(not a manual LICENSE review); NOASSERTION repos are unverified/unknown.",
        ],
    }
    (out / "exclusion-report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    return tasks


def main(argv: Sequence[str]) -> int:
    if argv[:1] == ["export"]:
        print(export_metadata(Path(argv[1]), Path(argv[2])))
        return 0
    tasks = prepare_swe_smith_manifest(Path(argv[0]), Path(argv[1]))
    print(len(tasks))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
