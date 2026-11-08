"""Warm-pod lifecycle: ``python -m research.training.podctl up|status|run|smoke|down``.

``up`` provisions one GPU pod (RTX 4090, then A6000/A40 fallback), installs the on-pod
deadman and idle shutdown and verifies the pinned base. ``down`` deletes the pod, verifies
it is gone and writes the paid lifetime ledger entry. State lives outside the repository.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from research.generator.decoding import POLICY
from research.generator.ledger import JsonlLedger, gpu_entry
from research.schemas.models import SINGLE_JOB_CHECKIN_USD, CostStatus
from research.training.runpod import (
    GPU_CHOICES,
    PRICING_REFERENCE,
    PodSession,
    RunpodClient,
    RunpodError,
    _wait_ready,
    attributed_entry,
    ensure_key,
    estimate_usd,
    verify_teardown,
)

STATE_DIR: Final = Path.home() / ".cache/proofhunter-research"
STATE = STATE_DIR / "pod.json"
REMOTE: Final = "/workspace/ph"
FALLBACK: Final = ("rtx4090", "a6000", "a40")


def _state() -> dict[str, object]:
    if not STATE.exists():
        raise SystemExit("no warm pod (run `up` first)")
    data: dict[str, object] = json.loads(STATE.read_text())
    return data


def attach(client: RunpodClient | None = None) -> PodSession:
    st = _state()
    client = client or RunpodClient()
    pod = _wait_ready(client, str(st["pod_id"]), timeout=120)
    return PodSession(client, str(st["pod_id"]), str(pod["publicIp"]),
                      int(pod["portMappings"]["22"]), ensure_key(),
                      float(str(st["cost_per_hr"])), float(str(st["started"])))


def stage_base(stage: Path) -> None:
    from research.training.qlora import LOCK, POD_DIR, model_pins

    for name in ("hf_generate.py", "train_qlora.py", "fetch_model.py", "bootstrap.sh"):
        shutil.copy(POD_DIR / name, stage / name)
    shutil.copy(LOCK, stage / "pod-requirements.lock")
    (stage / "model_pins.json").write_text(json.dumps(model_pins(), sort_keys=True))
    (stage / "policy.json").write_text(POLICY.model_dump_json())


def up(gpu: str, max_hours: float, idle_minutes: int, ledger: JsonlLedger) -> int:
    if STATE.exists():
        raise SystemExit(f"warm pod already recorded in {STATE}")
    client = RunpodClient()
    key = ensure_key()
    order = [gpu, *[g for g in FALLBACK if g != gpu]]
    from research.generator import pins

    for choice in order:
        estimate = estimate_usd(choice, max_hours)
        if estimate > SINGLE_JOB_CHECKIN_USD:
            raise SystemExit(f"estimate ${estimate} above check-in threshold")
        body = {
            "name": f"ph-generator-{int(time.time())}", "imageName": pins.POD_IMAGE,
            "cloudType": "SECURE", "computeType": "GPU", "gpuTypeIds": [GPU_CHOICES[choice][0]],
            "gpuCount": 1, "containerDiskInGb": 80, "volumeInGb": 0, "ports": ["22/tcp"],
            "supportPublicIp": True,
            "env": {"PUBLIC_KEY": key.with_suffix(".pub").read_text().strip()},
        }
        started = time.time()
        try:
            pod = client.create_pod(body)
        except RunpodError as exc:
            print(json.dumps({"gpu": choice, "create_failed": str(exc)}))
            continue
        pod_id = str(pod["id"])
        cost = float(pod.get("costPerHr") or GPU_CHOICES[choice][1])
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"pod_id": pod_id, "gpu": choice, "cost_per_hr": cost,
                                     "started": started, "max_hours": max_hours,
                                     "estimate_usd": estimate, "idle_minutes": idle_minutes}))
        print(json.dumps({"pod_id": pod_id, "gpu": choice, "cost_per_hr": cost}), flush=True)
        try:
            session = attach(client) if _wait_ready(client, pod_id) else None
            assert session is not None
            with tempfile.TemporaryDirectory() as tmp:
                stage_base(Path(tmp))
                session.run(f"mkdir -p {REMOTE}/base")
                for item in sorted(Path(tmp).iterdir()):
                    session.put(item, f"{REMOTE}/base/{item.name}")
            out = session.run(f"bash {REMOTE}/base/bootstrap.sh {int(max_hours * 3600)} "
                              f"{idle_minutes}", timeout=3600)
            print(out[-2000:])
        except Exception:
            down(ledger)
            raise
        return 0
    raise SystemExit("no GPU type could be provisioned")


def down(ledger: JsonlLedger) -> int:
    st = _state()
    client = RunpodClient()
    pod_id = str(st["pod_id"])
    client.delete_pod(pod_id)
    verified = verify_teardown(client, pod_id)
    hours = (time.time() - float(str(st["started"]))) / 3600
    ledger.append(gpu_entry(
        entry_id=f"pod-{pod_id}", provider="runpod", resource_id=pod_id, run_id=None,
        hours=hours, unit_price=float(str(st["cost_per_hr"])),
        estimated_job_usd=float(str(st["estimate_usd"])), pricing_reference=PRICING_REFERENCE,
        phase="training", wall_ms=round(hours * 3_600_000, 1), teardown_verified=verified,
        cost_status=CostStatus.ESTIMATED))
    print(json.dumps({"pod_id": pod_id, "hours": round(hours, 4), "teardown_verified": verified,
                      "remaining_pods": [p.get("id") for p in client.list_pods()]}))
    if not verified:
        raise SystemExit("teardown NOT verified")
    STATE.unlink()
    return 0


def smoke(run_id: str, out: Path, ledger: JsonlLedger) -> int:
    from research.training.qlora import SMOKE_CONFIG, QloraTrainer
    from research.training.smoke import smoke_examples

    st = _state()
    train, val = smoke_examples()
    trainer = QloraTrainer(run_id=run_id, config=SMOKE_CONFIG, ledger=ledger, pod=attach(),
                           max_hours=float(str(st["max_hours"])),
                           idle_minutes=int(str(st["idle_minutes"])))
    digest = trainer.train(train, val, out)
    print(json.dumps({"smoke_adapter_sha256": digest, "out": str(out)}))
    return 0


def full(run_id: str, out: Path, train_path: Path, val_path: Path, ledger: JsonlLedger) -> int:
    from research.schemas.models import GeneratorExample, Split
    from research.training.qlora import QloraTrainer, TrainConfig

    def read(path: Path, split: Split) -> list[GeneratorExample]:
        rows = [GeneratorExample.model_validate_json(x)
                for x in path.read_text().splitlines() if x.strip()]
        if any(r.split is not split for r in rows):
            raise SystemExit(f"{path} contains rows outside split {split.value}")
        return rows

    st = _state()
    trainer = QloraTrainer(run_id=run_id, config=TrainConfig(), ledger=ledger, pod=attach(),
                           max_hours=float(str(st["max_hours"])),
                           idle_minutes=int(str(st["idle_minutes"])))
    digest = trainer.train(read(train_path, Split.TRAIN), read(val_path, Split.VALIDATION), out)
    print(json.dumps({"adapter_sha256": digest, "out": str(out)}))
    return 0


def finish(run_id: str, out: Path, train_path: Path, val_path: Path, epoch: int,
           ledger: JsonlLedger) -> int:
    """Stop training after ``epoch`` checkpoints, evaluate them on validation and freeze."""
    from research.generator.model import GENERATIONS_FILE, sha256_file
    from research.schemas.models import GeneratorExample, Split
    from research.training.qlora import QloraTrainer, TrainConfig

    def read(path: Path, split: Split) -> list[GeneratorExample]:
        rows = [GeneratorExample.model_validate_json(x)
                for x in path.read_text().splitlines() if x.strip()]
        if any(r.split is not split for r in rows):
            raise SystemExit(f"{path} contains rows outside split {split.value}")
        return rows

    pod = attach()
    run_dir = f"{REMOTE}/runs/{run_id}"
    while f"epoch-{epoch}" not in pod.run(f"ls {run_dir}/ckpt 2>/dev/null || true"):
        time.sleep(30)
    time.sleep(20)
    pod.run(f"pkill -f train_qlora.py || true; touch {REMOTE}/keepalive")
    start = time.time()
    try:
        pod.get(f"{run_dir}/ckpt/train_log.json", out / "train_log.json")
        log = json.loads((out / "train_log.json").read_text())
        log["epochs"] = [e for e in log["epochs"] if e["epoch"] <= epoch]
        log["stopped_after_epoch"] = epoch
        (out / "train_log.json").write_text(json.dumps(log, indent=1))
        gen = (f"cd {run_dir} && python hf_generate.py --model-dir /workspace/model "
               f"--prompts val_prompts.jsonl --policy policy.json")
        for e in log["epochs"]:
            name = e["checkpoint"]
            pod.run(f"{gen} --adapter ckpt/{name} --out ckpt/{name}/{GENERATIONS_FILE}",
                    timeout=7200)
            pod.get(f"{run_dir}/ckpt/{name}", out / "checkpoints" / name)
    finally:
        (out / "finish_pod_log.txt").write_text("\n".join(pod.log))
        from research.training.runpod import attributed_entry
        ledger.append(attributed_entry(f"{run_id}-finish", pod.pod_id, time.time() - start))
    st = _state()
    trainer = QloraTrainer(run_id=run_id, config=TrainConfig(), ledger=ledger, pod=pod,
                           max_hours=float(str(st["max_hours"])))
    train_l, val_l = read(train_path, Split.TRAIN), read(val_path, Split.VALIDATION)
    with tempfile.TemporaryDirectory() as tmp:
        hashes = trainer.stage(train_l, val_l, Path(tmp))
        listing = "".join(f"{p.name}:{sha256_file(p)}\n" for p in sorted(Path(tmp).iterdir()))
    import hashlib
    hashes["stage_files_sha256"] = hashlib.sha256(listing.encode()).hexdigest()
    digest = trainer.freeze(trainer.select(val_l, out), val_l, out, hashes)
    print(json.dumps({"adapter_sha256": digest, "out": str(out)}))
    return 0


def _read(path: Path, split: object) -> list[object]:
    from research.schemas.models import GeneratorExample

    rows = [GeneratorExample.model_validate_json(x)
            for x in path.read_text().splitlines() if x.strip()]
    if any(r.split is not split for r in rows):
        raise SystemExit(f"{path} contains rows outside split {split}")
    return list(rows)


def _with_state(name: str) -> None:
    global STATE
    STATE = STATE_DIR / f"pod-{name}.json"


def launch_train(run_id: str, train_path: Path, val_path: Path, out: Path) -> int:
    """Stage the run on the train pod and start QLoRA detached (survives SSH drops)."""
    from research.schemas.models import GeneratorExample, Split
    from research.training.qlora import QloraTrainer, TrainConfig

    train_l = [r for r in _read(train_path, Split.TRAIN) if isinstance(r, GeneratorExample)]
    val_l = [r for r in _read(val_path, Split.VALIDATION) if isinstance(r, GeneratorExample)]
    pod = attach()
    trainer = QloraTrainer(run_id=run_id, config=TrainConfig(), pod=pod)
    run_dir = f"{REMOTE}/runs/{run_id}"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        hashes = trainer.stage(train_l, val_l, Path(tmp))
        (out / "stage_hashes.json").write_text(json.dumps(hashes, indent=1, sort_keys=True))
        pod.run(f"mkdir -p {run_dir}")
        for item in sorted(Path(tmp).iterdir()):
            pod.put(item, f"{run_dir}/{item.name}")
    pod.run(f"cd {run_dir} && nohup python train_qlora.py --model-dir /workspace/model "
            f"--data sft.jsonl --config config.json --out ckpt > train.out 2>&1 &")
    print(json.dumps({"launched": run_id, "pod": pod.pod_id}))
    return 0


def watch(run_id: str, out: Path, val_path: Path, ledger: JsonlLedger) -> int:
    """Evaluate each epoch checkpoint from the train pod on the eval pod as it appears."""
    from research.generator.model import GENERATIONS_FILE, adapter_sha256, load_generations
    from research.generator.scoring import score_generations
    from research.schemas.models import GeneratorExample, Split

    val = [r for r in _read(val_path, Split.VALIDATION) if isinstance(r, GeneratorExample)]
    _with_state("train")
    tpod = attach()
    _with_state("eval")
    epod = attach()
    run_dir = f"{REMOTE}/runs/{run_id}"
    stage = out / "eval_stage"
    if not stage.exists():
        stage.mkdir(parents=True)
        tpod.get(f"{run_dir}/val_prompts.jsonl", stage / "val_prompts.jsonl")
        tpod.get(f"{run_dir}/hf_generate.py", stage / "hf_generate.py")
        tpod.get(f"{run_dir}/policy.json", stage / "policy.json")
        epod.run(f"mkdir -p {run_dir}/ckpt")
        for item in sorted(stage.iterdir()):
            epod.put(item, f"{run_dir}/{item.name}")
    done = {p.parent.name for p in (out / "checkpoints").glob(f"*/{GENERATIONS_FILE}")}
    start = time.time()
    try:
        while True:
            raw = tpod.run(f"cat {run_dir}/ckpt/train_log.json 2>/dev/null || echo null")
            log = json.loads(raw) if raw.strip() != "null" else None
            prog = tpod.run(f"cat {run_dir}/ckpt/progress.json 2>/dev/null; "
                            f"pgrep -f train_qlora.py >/dev/null && echo ' alive' || echo ' dead'")
            print(json.dumps({"t": time.strftime("%H:%M:%S"), "progress": prog.strip()}),
                  flush=True)
            for ep in (log or {}).get("epochs", []):
                name = ep["checkpoint"]
                if name in done:
                    continue
                local = out / "checkpoints" / name
                tpod.get(f"{run_dir}/ckpt/{name}", local)
                epod.put(local, f"{run_dir}/ckpt/{name}")
                epod.run(f"cd {run_dir} && python hf_generate.py --model-dir /workspace/model "
                         f"--prompts val_prompts.jsonl --policy policy.json --adapter ckpt/{name} "
                         f"--out ckpt/{name}/{GENERATIONS_FILE}", timeout=7200)
                epod.get(f"{run_dir}/ckpt/{name}/{GENERATIONS_FILE}", local / GENERATIONS_FILE)
                score = score_generations(val, load_generations(local / GENERATIONS_FILE))
                rec = {"checkpoint": name, "adapter_sha256": adapter_sha256(local),
                       "val_loss": ep["val_loss"], **score.__dict__}
                (local / "validation_score.json").write_text(json.dumps(rec, indent=1))
                print(json.dumps({"EVAL": rec}), flush=True)
                done.add(name)
            if log is not None:
                (out / "train_log.json").write_text(json.dumps(log, indent=1))
            if log is not None and "wall_s" in log and all(
                    e["checkpoint"] in done for e in log["epochs"]):
                return 0
            if "dead" in prog and (log is None or "wall_s" not in log):
                raise SystemExit("training process died before finishing")
            time.sleep(120)
    finally:
        ledger.append(attributed_entry(f"{run_id}-eval-{int(start)}", epod.pod_id,
                                       time.time() - start))


def freeze_best(run_id: str, out: Path, train_path: Path, val_path: Path) -> int:
    from research.schemas.models import GeneratorExample, Split
    from research.training.qlora import QloraTrainer, TrainConfig

    val = [r for r in _read(val_path, Split.VALIDATION) if isinstance(r, GeneratorExample)]
    trainer = QloraTrainer(run_id=run_id, config=TrainConfig())
    hashes = json.loads((out / "stage_hashes.json").read_text())
    digest = trainer.freeze(trainer.select(val, out), val, out, hashes)
    print(json.dumps({"adapter_sha256": digest}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="podctl")
    ap.add_argument("action", choices=["up", "status", "run", "smoke", "train", "finish", "down",
                                       "launch-train", "watch", "freeze"])
    ap.add_argument("--gpu", choices=sorted(GPU_CHOICES), default="rtx4090")
    ap.add_argument("--max-hours", type=float, default=8.0)
    ap.add_argument("--idle-minutes", type=int, default=45)
    ap.add_argument("--ledger", type=Path, default=JsonlLedger().path)
    ap.add_argument("--run-id", default="smoke-1")
    ap.add_argument("--out", type=Path, default=Path("research/artifacts/generator/smoke"))
    ap.add_argument("--cmd", default="nvidia-smi")
    ap.add_argument("--train", type=Path)
    ap.add_argument("--val", type=Path)
    ap.add_argument("--epoch", type=int, default=1)
    ap.add_argument("--name", default=None, help="pod state name (train, eval)")
    args = ap.parse_args(argv)
    ledger = JsonlLedger(args.ledger)
    if args.name:
        _with_state(args.name)
    if args.action == "launch-train":
        return launch_train(args.run_id, args.train, args.val, args.out)
    if args.action == "watch":
        return watch(args.run_id, args.out, args.val, ledger)
    if args.action == "freeze":
        return freeze_best(args.run_id, args.out, args.train, args.val)
    if args.action == "up":
        return up(args.gpu, args.max_hours, args.idle_minutes, ledger)
    if args.action == "down":
        return down(ledger)
    if args.action == "smoke":
        return smoke(args.run_id, args.out, ledger)
    if args.action == "finish":
        return finish(args.run_id, args.out, args.train, args.val, args.epoch, ledger)
    if args.action == "train":
        return full(args.run_id, args.out, args.train, args.val, ledger)
    session = attach()
    if args.action == "run":
        print(session.run(f"touch {REMOTE}/keepalive; {args.cmd}", timeout=7200))
    else:
        print(json.dumps(_state()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
