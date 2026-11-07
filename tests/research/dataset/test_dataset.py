from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from research.data import execute as X
from research.data import observations, sft
from research.data import templates as T
from research.data.build import build_cases
from research.data.store import SealedLabelsError, open_store, unseal_token
from research.data.swe_smith import prepare_swe_smith_manifest
from research.schemas import models as M

ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = M.ProtocolV1.model_validate_json((ROOT / "research/spec/protocol-v1.json").read_bytes())
SMALL = {
    "train": M.SplitTarget(defect_cases=60, clean_controls=10, min_families=12),
    "validation": M.SplitTarget(defect_cases=20, clean_controls=4, min_families=3),
    "test": M.SplitTarget(defect_cases=30, clean_controls=6, min_families=5),
}


@pytest.fixture(scope="module")
def small_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("track_a")
    protocol = PROTOCOL.model_copy(update={"split_targets": SMALL})
    build_cases(protocol, root, workers=2, dataset_commit="1" * 40)
    return root


def test_templates_are_split_owned_and_deterministic() -> None:
    infos = T.list_templates()
    assert len({i.template_id for i in infos}) == len(infos)
    for split, minimum in (("train", 12), ("validation", 3), ("test", 5)):
        fams = {i.family_id for i in infos if i.kind == "defect" and i.split.value == split}
        assert len(fams) >= minimum
    params = {"value": 7, "cond": "sub_ge", "cond_value": 500, "names": 1}
    assert T.render("offset_low_rounding__s01", params) == T.render("offset_low_rounding__s01",
                                                                      params)
    with pytest.raises(ValueError):
        T.render("offset_low_rounding__s08", params)  # family and shape in different splits
    with pytest.raises(ValueError):
        T.render("offset_low_rounding__s01", dict(params, value=50))  # outside reviewed domain


def test_every_clean_shape_matches_trusted_oracle() -> None:
    probes = X.probe_set(PROTOCOL.replicate_seeds, PROTOCOL.master_seed)[:600]
    for shape in T.SHAPES:
        for guard in (0, 1):
            for names in range(3):
                base, head = T.render(f"clean__{shape.shape_id}",
                                      {"names": names, "guard": guard, "head_names": 2 - names,
                                       "head_subtotal": "while", "head_numerator": "split"})
                for source in (base, head):
                    fn = X.load_entry(source)
                    assert all(X.call(fn, p) == X.expected(p) for p in probes), shape


def test_manifest_grouped_labels_witnessed_and_test_sealed(small_root: Path) -> None:
    store = open_store(small_root)
    manifest = store.manifest()
    assert manifest.split_policy == "grouped-family-origin-cluster-v1"
    counts = {s: sum(c.split is s for c in manifest.cases) for s in M.Split}
    assert counts == {M.Split.TRAIN: 70, M.Split.VALIDATION: 24, M.Split.TEST: 36}
    for split in ("train", "validation"):
        for label in store.labels(split):
            src = store.sources(label.case_id)
            if label.witness is None:
                continue
            item = (tuple(label.witness.input.prices_cents), label.witness.input.discount_percent)
            assert X.call(X.load_entry(src.head_source), item) == label.witness.head_cents
            assert X.call(X.load_entry(src.base_source), item) == label.witness.base_cents
            assert X.expected(item) == label.witness.expected_cents != label.witness.head_cents
    with pytest.raises(SealedLabelsError):
        store.labels("test")
    with pytest.raises(SealedLabelsError):
        store.labels("test", unseal="research-protocol-v1:" + "0" * 64)
    sealed = small_root / "labels" / "test.sealed.json"
    import hashlib

    token = unseal_token(hashlib.sha256(sealed.read_bytes()).hexdigest())
    assert len(store.labels("test", unseal=token)) == counts[M.Split.TEST]
    audit = json.loads((small_root / "reports" / "split-audit.json").read_text())
    assert all(not v for v in audit["grouping_key_violations"].values())
    assert audit["source_sha256_shared_across_splits"] == 0
    assert audit["behavior_fingerprint_shared_across_splits"] == 0


def test_build_is_deterministic(small_root: Path, tmp_path: Path) -> None:
    protocol = PROTOCOL.model_copy(update={"split_targets": SMALL})
    again = build_cases(protocol, tmp_path, workers=2, dataset_commit="1" * 40)
    assert again.cases_sha256 == open_store(small_root).manifest().cases_sha256


def test_observations_and_generator_examples(small_root: Path) -> None:
    store = open_store(small_root)
    rows = observations.collect(store, M.Split.VALIDATION, PROTOCOL.replicate_seeds)
    per_case: dict[str, int] = {}
    for row in rows:
        per_case[row.case_id] = per_case.get(row.case_id, 0) + 1
        assert row.selection_probability is not None and 0 < row.selection_probability <= 1
    assert max(per_case.values()) <= 20
    assert any(r.relevance is M.Relevance.POSITIVE for r in rows)
    with pytest.raises(ValueError):
        observations.collect(store, M.Split.TEST, PROTOCOL.replicate_seeds)
    examples = sft.build_examples(store, M.Split.TRAIN)
    assert examples
    for ex in examples:
        case = next(c for c in store.manifest().cases if c.case_id == ex.case_id)
        text = ex.input.model_dump_json()
        assert case.family_id not in text and case.template_id not in text
        assert "cond_value" not in text and "params" not in text
    with pytest.raises(ValueError):
        sft.build_examples(store, M.Split.TEST)


def _meta_row(iid: str, repo: str, f2p: list[str], files: list[str], sha: str) -> dict[str, object]:
    return {"instance_id": iid, "repo": repo, "image_name": "img", "FAIL_TO_PASS": f2p,
            "PASS_TO_PASS": ["tests/test_a.py::test_ok", "bad id with spaces"],
            "patch_files": files, "patch_sha256": sha}


def test_swe_smith_metadata_only(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    repo_a, repo_b = "swesmith/o__a.1234abcd", "swesmith/o__b.1234abcd"
    rows = [
        _meta_row("o__a.1234abcd.func_basic__x1", repo_a, ["tests/test_a.py::test_x"],
                  ["a/core.py"], "1"),
        _meta_row("o__a.1234abcd.lm_rewrite__x2", repo_a, ["tests/test_a.py::test_y"],
                  ["a/core.py"], "2"),
        _meta_row("o__b.1234abcd.func_basic__x3", repo_b, ["tests/test_b.py::test_z"],
                  ["tests/test_b.py"], "3"),
        _meta_row("o__b.1234abcd.func_basic__x4", repo_b, ["tests/test_b.py::test_q"],
                  ["b/core.py"], "1"),
        _meta_row("o__b.1234abcd.func_basic__x5", repo_b, ["tests/test_b.py::test_w"],
                  ["b/core.py"], "5"),
    ]
    (src / "metadata.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    pin = {"upstream_license_spdx_github": "MIT", "mirror_main_sha": "a" * 40}
    (src / "pins.json").write_text(json.dumps({"swe_smith_py_repos": {repo_a: pin, repo_b: pin}}))
    (src / "split_proposal.json").write_text(json.dumps({"splits": {
        "train": {repo_a: ["o__a.1234abcd.func_basic__x1"]},
        "validation": {}, "test": {repo_b: ["o__b.1234abcd.func_basic__x5"]}}}))
    (src / "shards.json").write_text(json.dumps(
        {"revision": "77cab9055d42ab4a5c25c89a8f937096db13558e", "files": {"x.parquet": "0" * 64}}))
    tasks = prepare_swe_smith_manifest(src, tmp_path / "out")
    assert {(t.instance_id, t.split.value) for t in tasks} == {
        ("o__a.1234abcd.func_basic__x1", "train"), ("o__b.1234abcd.func_basic__x5", "test")}
    assert all(not t.executed and t.repo_commit == "a" * 40 for t in tasks)
    report = json.loads((tmp_path / "out" / "exclusion-report.json").read_text())
    assert report["rejections"]["lm_rewrite_label_noise"] == 1
    assert report["rejections"]["patch_touches_tests"] == 1
    assert report["rejections"]["duplicate_patch"] == 1


def test_track_b_module_cannot_execute_code() -> None:
    tree = ast.parse((ROOT / "research/data/swe_smith.py").read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"subprocess", "os", "importlib", "runpy", "docker", "multiprocessing"}
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)}
    assert not calls & {"exec", "eval", "compile", "__import__"}
