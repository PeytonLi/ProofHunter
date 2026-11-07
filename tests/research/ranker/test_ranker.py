from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("lightgbm")

from research.ranker import collect, commands, executor, features, fixture
from research.ranker.selector import load_ranker_selector
from research.ranker.train import LambdaRankTrainer
from research.schemas import interfaces as I
from research.schemas import models as M

SEEDS = collect.replicate_seeds()[:1]


@pytest.fixture(scope="module")
def store() -> fixture.FixtureStore:
    return fixture.FixtureStore()


@pytest.fixture(scope="module")
def rows(store: fixture.FixtureStore) -> dict[str, list[M.RankingObservation]]:
    return {s: list(collect.collect_observations(store, M.Split(s), SEEDS, "eig_eps0.3",
                                                 reviewer=fixture.verify_fixture))
            for s in ("train", "validation")}


@pytest.fixture(scope="module")
def checkpoint(rows: dict[str, list[M.RankingObservation]],
               tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    out = tmp_path_factory.mktemp("ck") / "full"
    return out, LambdaRankTrainer("full").train(rows["train"], rows["validation"], out)


def test_feature_names_are_pre_execution() -> None:
    assert not [n for n in features.FEATURE_NAMES if n.startswith(M.FORBIDDEN_FEATURE_PREFIXES)]
    assert isinstance(features.CheckoutFeatures(), I.FeatureExtractor)
    nb = features.feature_names("no_belief")
    assert not set(nb) & set(features.GROUPS["belief"])
    assert set(features.feature_names()) - set(nb) == set(features.GROUPS["belief"])


def test_collected_rows_valid_grouped_and_deterministic(
        store: fixture.FixtureStore, rows: dict[str, list[M.RankingObservation]]) -> None:
    train = rows["train"]
    assert train
    assert all(r.split is M.Split.TRAIN for r in train)
    assert {r.relevance for r in train} >= {M.Relevance.POSITIVE, M.Relevance.ZERO}
    assert all(r.selection_probability is not None for r in train)
    assert all(sorted(r.features) == sorted(features.FEATURE_NAMES) for r in train)
    groups: dict[tuple[str, int], int] = {}
    for r in train:
        groups[(r.case_id, r.decision_step)] = groups.get((r.case_id, r.decision_step), 0) + 1
    assert max(groups.values()) > 1
    again = list(collect.collect_observations(store, M.Split.TRAIN, SEEDS, "eig_eps0.3",
                                              reviewer=fixture.verify_fixture))
    assert [r.model_dump(exclude={"cost"}) for r in again] == [
        r.model_dump(exclude={"cost"}) for r in train]
    assert store.label_reads == []


def test_collection_refuses_test_split_and_unreviewed(store: fixture.FixtureStore) -> None:
    with pytest.raises(PermissionError):
        next(collect.collect_observations(store, M.Split.TEST, SEEDS, "eig_eps0.3",
                                          reviewer=fixture.verify_fixture))

    def reject(_: I.CaseSources) -> None:
        raise executor.UnreviewedSourceError("no")

    with pytest.raises(executor.UnreviewedSourceError):
        next(collect.collect_observations(store, M.Split.TRAIN, SEEDS, "eig_eps0.3",
                                          reviewer=reject))
    with pytest.raises(ValueError):
        collect.epsilon("eig_eps0")


def test_unusable_rows_are_unknown(store: fixture.FixtureStore) -> None:
    def broken(source: bytes, calls: object) -> tuple[list[executor.CallResult], float]:
        n = len(calls)  # type: ignore[arg-type]
        return [executor.CallResult(None, "timeout")] * n, 1.0

    out = list(collect.collect_observations(store, M.Split.TRAIN, SEEDS, "eig_eps0.3",
                                            reviewer=fixture.verify_fixture, runner=broken))
    assert out
    assert {r.relevance for r in out} == {M.Relevance.UNKNOWN}
    assert {r.unusable_reason for r in out} == {"timeout"}


def test_executor_isolated() -> None:
    src = (b"import os\ndef total_cents(p, d):\n"
           b"    return int(set(os.environ) <= {'LC_CTYPE'})\n")
    (res,), _ = executor.run_batch(src, [([1], 0)])
    assert res.value == 1
    (res,), _ = executor.run_batch(b"def total_cents(p, d):\n    return -1\n", [([1], 0)])
    assert res.error == "invalid_output"


def test_training_deterministic_and_rejects_wrong_split(
        rows: dict[str, list[M.RankingObservation]], checkpoint: tuple[Path, str],
        tmp_path: Path) -> None:
    out, digest = checkpoint
    again = LambdaRankTrainer("full").train(rows["train"], rows["validation"], tmp_path / "b")
    assert again == digest
    assert (tmp_path / "b" / "model.txt").read_bytes() == (out / "model.txt").read_bytes()
    with pytest.raises(PermissionError):
        LambdaRankTrainer().train(rows["validation"], rows["validation"], tmp_path / "c")
    meta = json.loads((out / "checkpoint.json").read_text())
    assert meta["lightgbm_version"] == "4.7.0"


def test_tuning_uses_validation_only(rows: dict[str, list[M.RankingObservation]],
                                     tmp_path: Path) -> None:
    trainer = LambdaRankTrainer("full")
    trainer.train(rows["train"], rows["validation"], tmp_path)
    trials = trainer.report["trials"]
    best = max(t["validation"]["mrr"] for t in trials)
    assert trainer.report["validation_metrics"]["mrr"] == pytest.approx(best)


def test_selector_conforms_and_verifies_hash(
        store: fixture.FixtureStore, checkpoint: tuple[Path, str], tmp_path: Path) -> None:
    out, _ = checkpoint
    sel = load_ranker_selector(out, arm_id="learned_ranker")
    assert isinstance(sel, I.Selector)
    assert sel.arm_id == "learned_ranker"
    assert isinstance(load_ranker_selector(out / "checkpoint.json", arm_id="a"), I.Selector)
    assert getattr(sel, "checkpoint_sha256", None) == checkpoint[1]
    case = next(c for c in store.manifest().cases if c.split is M.Split.VALIDATION)
    ctx = collect.context_for(store.sources(case.case_id), SEEDS[0])
    state = I.EpisodeState(step=1, remaining_pairs=6, available=ctx.bank.candidates,
                           missing_coverage=(), history=())
    sel.reset(ctx)
    pick = sel.choose(state)
    assert pick in {c.input_sha256 for c in ctx.bank.candidates}
    assert sel.choose(state) == pick
    bad = tmp_path / "bad"
    bad.mkdir()
    for name in ("checkpoint.json", "model.txt"):
        (bad / name).write_bytes((out / name).read_bytes())
    (bad / "model.txt").write_text((out / "model.txt").read_text() + "\n")
    with pytest.raises(ValueError):
        load_ranker_selector(bad, arm_id="x")


def test_ablation_without_belief(rows: dict[str, list[M.RankingObservation]],
                                 tmp_path: Path) -> None:
    trainer = LambdaRankTrainer("no_belief")
    trainer.train(rows["train"], rows["validation"], tmp_path)
    meta = json.loads((tmp_path / "checkpoint.json").read_text())
    assert not set(meta["feature_names"]) & set(features.GROUPS["belief"])
    assert not {n for n, _ in trainer.report["top_gain"]} & set(features.GROUPS["belief"])


def test_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed = str(SEEDS[0])
    for split in ("train", "validation"):
        assert commands.collect_observations(
            ["--split", split, "--seeds", seed, "--out", str(tmp_path / f"{split}.jsonl")]) == 0
    assert commands.train_ranker(["--train", str(tmp_path / "train.jsonl"), "--validation",
                                  str(tmp_path / "validation.jsonl"), "--out",
                                  str(tmp_path / "ck"), "--ablation", "full"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert json.loads(out[-1])["full"]["checkpoint_sha256"]


def test_ablation_choices_match() -> None:
    assert set(commands.ABLATION_CHOICES) == set(features.ABLATIONS)




def _logged(rows: list[M.RankingObservation]) -> list[M.RankingObservation]:
    """One row per decision point, as in the dataset-lane log (first candidate by hash)."""
    seen: dict[tuple[str, int, int], M.RankingObservation] = {}
    for r in sorted(rows, key=lambda r: r.candidate.input_sha256):
        seen.setdefault((r.case_id, r.episode_seed, r.decision_step), r)
    return list(seen.values())


def test_episode_grouping_and_ips_weights(
        rows: dict[str, list[M.RankingObservation]]) -> None:
    from research.ranker import metrics

    logged = _logged(rows["train"])
    names = features.feature_names("full")
    decision = metrics.build(logged, names, features.FEATURE_SET_VERSION,
                             allowed=(M.Split.TRAIN,))
    assert not decision.sizes
    ep = metrics.build(logged, names, features.FEATURE_SET_VERSION, allowed=(M.Split.TRAIN,),
                       group_by="episode", weighting="ips")
    assert ep.sizes
    assert all(k[3] == 0 for k in ep.keys)
    assert abs(float(ep.w.mean()) - 1.0) < 1e-9
    raw = [metrics.ips_weight(r) for r in logged if r.relevance is not M.Relevance.UNKNOWN]
    assert max(raw) <= metrics.IPS_CLIP
    flat = metrics.build(logged, names, features.FEATURE_SET_VERSION, allowed=(M.Split.TRAIN,),
                         group_by="episode")
    assert set(flat.w.tolist()) == {1.0}


def test_logged_ips_training_records_design(rows: dict[str, list[M.RankingObservation]],
                                            tmp_path: Path) -> None:
    logged = _logged(rows["train"])
    trainer = LambdaRankTrainer("full", group_by="episode", weighting="ips")
    trainer.train(logged, rows["validation"], tmp_path / "ips")
    meta = json.loads((tmp_path / "ips" / "checkpoint.json").read_text())
    assert (meta["train_group_by"], meta["train_weighting"]) == ("episode", "ips")
    assert load_ranker_selector(tmp_path / "ips", arm_id="builtin_ranker").arm_id


def test_refeaturize_checks_pool(store: fixture.FixtureStore,
                                 rows: dict[str, list[M.RankingObservation]]) -> None:
    from research.ranker.relabel import refeaturize

    with pytest.raises(ValueError, match="pool mismatch"):
        list(refeaturize(store, _logged(rows["train"])[:3]))
    test_row = rows["train"][0].model_copy(update={"split": M.Split.TEST})
    with pytest.raises(PermissionError):
        list(refeaturize(store, [test_row]))


def test_compare_reports_spec_arms(rows: dict[str, list[M.RankingObservation]],
                                   checkpoint: tuple[Path, str]) -> None:
    from research.ranker.compare import compare

    report = compare(rows["validation"], {"ranker": checkpoint[0]}, "ranker")
    assert set(report["arms"]) == {"fixed_order", "uniform_random", "simple_heuristic",
                                   "builtin_eig", "ranker"}
    assert all(0 < a["mrr"] <= 1 for a in report["arms"].values())
    ci = report["paired_vs_primary"]["uniform_random"]
    assert ci["ci95_low"] <= ci["mrr_diff"] <= ci["ci95_high"]
    assert report["checkpoints"]["ranker"]["checkpoint_sha256"] == checkpoint[1]


def test_v1_augment_matches_v2_extractor(rows: dict[str, list[M.RankingObservation]]) -> None:
    from research.ranker.relabel import augment_v1

    heur = set(features.GROUPS["heuristic"])
    v1 = [r.model_copy(update={"feature_set_version": features.V1,
                               "features": {k: v for k, v in r.features.items()
                                            if k not in heur}})
          for r in rows["train"]]
    assert augment_v1(v1) == rows["train"]


def test_heuristic_rank_matches_spec_arm(rows: dict[str, list[M.RankingObservation]]) -> None:
    from research.ranker.compare import heuristic_key

    group: dict[tuple[str, int, int], list[M.RankingObservation]] = {}
    for r in rows["validation"]:
        group.setdefault((r.case_id, r.episode_seed, r.decision_step), []).append(r)
    for g in group.values():
        top = min(g, key=lambda r: heuristic_key(r.candidate))
        assert top.features["heuristic_rank"] == 0.0
        assert top.features["heuristic_is_top"] == 1.0


def test_selector_loads_frozen_real_v1() -> None:
    ckpt = Path("research/artifacts/ranker/real-v1/checkpoints/dense_full")
    if not ckpt.exists():
        pytest.skip("real-v1 artifacts not present")
    sel = load_ranker_selector(ckpt, arm_id="builtin_ranker")
    assert getattr(sel, "_names", None) == features.feature_names("full", features.V1)
