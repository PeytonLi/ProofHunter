"""Pod-side QLoRA SFT (standalone). Loss on completion tokens only; prompts never truncated.

Rows: {"id", "split", "prompt", "completion"}. Saves a checkpoint per epoch, scores each on
validation generations (via the caller), and writes ``train_log.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path
from typing import Any


def encode(tok: Any, rows: list[dict[str, Any]], max_len: int) -> tuple[list[dict[str, Any]], int]:
    out, dropped = [], 0
    for row in rows:
        p = tok(row["prompt"], add_special_tokens=False).input_ids
        c = tok(row["completion"] + "<|im_end|>\n", add_special_tokens=False).input_ids
        if len(p) + len(c) > max_len:
            dropped += 1
            continue
        out.append({"input_ids": p + c, "labels": [-100] * len(p) + c})
    return out, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text())

    import torch  # type: ignore[import-not-found]
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training  # type: ignore[import-not-found]
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # type: ignore[import-not-found]

    random.seed(cfg["seed"])
    torch.manual_seed(cfg["seed"])
    tok = AutoTokenizer.from_pretrained(args.model_dir)
    rows = [json.loads(x) for x in Path(args.data).read_text().splitlines() if x.strip()]
    train = [r for r in rows if r["split"] == "train"]
    val = [r for r in rows if r["split"] == "validation"]
    train_enc, dropped_train = encode(tok, train, cfg["max_seq_tokens"])
    val_enc, dropped_val = encode(tok, val, cfg["max_seq_tokens"])
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_use_double_quant=True,
                             bnb_4bit_compute_dtype=torch.bfloat16)
    model = AutoModelForCausalLM.from_pretrained(args.model_dir, quantization_config=bnb,
                                                 torch_dtype=torch.bfloat16, device_map="cuda:0")
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(
        r=cfg["lora_r"], lora_alpha=cfg["lora_alpha"], lora_dropout=cfg["lora_dropout"],
        target_modules=cfg["target_modules"], bias="none", task_type="CAUSAL_LM"))
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=cfg["learning_rate"], weight_decay=0.0)
    steps_per_epoch = math.ceil(len(train_enc) / cfg["grad_accum"])
    total = max(1, min(cfg.get("max_steps") or 10**9, steps_per_epoch * cfg["epochs"]))
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / max(1, cfg["warmup_steps"])) * max(0.0, 1 - s / total))

    def loss_of(item: dict[str, Any]) -> Any:
        ids = torch.tensor([item["input_ids"]], device=model.device)
        labels = torch.tensor([item["labels"]], device=model.device)
        return model(input_ids=ids, labels=labels).loss

    log: dict[str, Any] = {"config": cfg, "train_rows": len(train_enc), "val_rows": len(val_enc),
                           "dropped_train": dropped_train, "dropped_val": dropped_val,
                           "steps": [], "epochs": []}
    step, t0 = 0, time.time()
    model.train()
    for epoch in range(cfg["epochs"]):
        order = list(range(len(train_enc)))
        random.shuffle(order)
        for i, idx in enumerate(order):
            loss = loss_of(train_enc[idx]) / cfg["grad_accum"]
            loss.backward()
            if (i + 1) % cfg["grad_accum"] == 0 or i + 1 == len(order):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                sched.step()
                opt.zero_grad()
                step += 1
                log["steps"].append({"step": step, "loss": float(loss) * cfg["grad_accum"],
                                     "t": round(time.time() - t0, 1)})
                if step >= total:
                    break
        model.eval()
        with torch.no_grad():
            vl = [float(loss_of(v)) for v in val_enc]
        model.train()
        ckpt = Path(args.out) / f"epoch-{epoch + 1}"
        model.save_pretrained(ckpt, safe_serialization=True)
        log["epochs"].append({"epoch": epoch + 1, "step": step, "checkpoint": ckpt.name,
                              "val_loss": sum(vl) / len(vl) if vl else None})
        Path(args.out, "train_log.json").write_text(json.dumps(log, indent=1))
        if step >= total:
            break
    log["wall_s"] = round(time.time() - t0, 1)
    Path(args.out, "train_log.json").write_text(json.dumps(log, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
