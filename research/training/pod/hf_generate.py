"""Pod-side greedy generation for both arms (standalone: torch/transformers/peft only).

Input JSONL rows: {"id", "prompt_sha256", "prompt"} where ``prompt`` is the exact rendered
ChatML text. Output rows: {"id", "prompt_sha256", "text", "finish_reason", "input_tokens",
"output_tokens", "gen_ms"}. Prompts longer than the policy budget are excluded, not truncated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


def load(model_dir: str, adapter: str | None, dtype: str = "bfloat16") -> tuple[Any, Any]:
    import torch  # type: ignore[import-not-found]
    from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore[import-not-found]

    tok = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForCausalLM.from_pretrained(
        model_dir, torch_dtype=getattr(torch, dtype), device_map="cuda:0")
    if adapter:
        from peft import PeftModel  # type: ignore[import-not-found]
        model = PeftModel.from_pretrained(model, adapter)
    model.eval()
    return tok, model


def generate_rows(tok: Any, model: Any, rows: list[dict[str, Any]], policy: dict[str, Any],
                  batch: int = 8) -> list[dict[str, Any]]:
    import torch

    tok.padding_side = "left"
    out: list[dict[str, Any]] = []
    eos = tok.convert_tokens_to_ids("<|im_end|>")
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        keep = []
        for row in chunk:
            if hashlib.sha256(row["prompt"].encode()).hexdigest() != row["prompt_sha256"]:
                raise ValueError(f"prompt hash mismatch for {row['id']}")
            n = len(tok(row["prompt"], add_special_tokens=False).input_ids)
            if n > policy["max_prompt_tokens"]:
                out.append({"id": row["id"], "prompt_sha256": row["prompt_sha256"], "text": "",
                            "finish_reason": "excluded_prompt_too_long", "input_tokens": n,
                            "output_tokens": 0, "gen_ms": 0.0})
            else:
                keep.append(row)
        if not keep:
            continue
        enc = tok([r["prompt"] for r in keep], return_tensors="pt", padding=True,
                  add_special_tokens=False).to(model.device)
        torch.manual_seed(policy["seed"])
        t0 = time.perf_counter()
        with torch.no_grad():
            gen = model.generate(**enc, do_sample=False, num_beams=1,
                                 max_new_tokens=policy["max_new_tokens"],
                                 repetition_penalty=policy["repetition_penalty"],
                                 eos_token_id=eos, pad_token_id=tok.pad_token_id,
                                 temperature=None, top_p=None, top_k=None)
        ms = (time.perf_counter() - t0) * 1000 / len(keep)
        width = enc.input_ids.shape[1]
        for i, row in enumerate(keep):
            ids = gen[i, width:].tolist()
            stopped = eos in ids
            ids = ids[:ids.index(eos)] if stopped else [t for t in ids if t != tok.pad_token_id]
            out.append({"id": row["id"], "prompt_sha256": row["prompt_sha256"],
                        "text": tok.decode(ids, skip_special_tokens=True),
                        "finish_reason": "stop" if stopped else "length",
                        "input_tokens": int(enc.attention_mask[i].sum()),
                        "output_tokens": len(ids), "gen_ms": round(ms, 3)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--adapter")
    ap.add_argument("--prompts", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    policy = json.loads(Path(args.policy).read_text())
    rows = [json.loads(x) for x in Path(args.prompts).read_text().splitlines() if x.strip()]
    tok, model = load(args.model_dir, args.adapter, policy["dtype"])
    result = generate_rows(tok, model, rows, policy)
    Path(args.out).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
