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
    ensure_key,
    estimate_usd,
    verify_teardown,
)

STATE: Final = Path.home() / ".cache/proofhunter-research/pod.json"
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


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="podctl")
    ap.add_argument("action", choices=["up", "status", "run", "smoke", "train", "finish", "down"])
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
    args = ap.parse_args(argv)
    ledger = JsonlLedger(args.ledger)
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
