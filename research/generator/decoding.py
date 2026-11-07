"""Frozen decoding and context policy, identical for the untuned control and the tuned model."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict


class DecodingPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = "generator-decoding@1"
    do_sample: bool = False
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int = 0
    repetition_penalty: float = 1.0
    num_beams: int = 1
    max_new_tokens: int = 1024
    #: Prompt token budget after the pinned chat template; longer prompts are excluded
    #: (never truncated) and the exclusion is reported for both arms.
    max_prompt_tokens: int = 4096
    stop: tuple[str, ...] = ("<|im_end|>",)
    #: Inference dtype for both arms; the LoRA adapter is applied unmerged.
    dtype: str = "bfloat16"
    seed: int = 1234


POLICY: Final = DecodingPolicy()
