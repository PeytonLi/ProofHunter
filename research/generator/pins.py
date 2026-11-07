"""Pinned base model, tokenizer, container and pod dependencies (frozen for control and tuned)."""

from __future__ import annotations

from typing import Final

BASE_MODEL: Final = "Qwen/Qwen2.5-Coder-7B-Instruct"
BASE_REVISION: Final = "c03e6d358207e414f1eca0bb1891e29f1db0e242"
TOKENIZER_REVISION: Final = BASE_REVISION
LICENSE: Final = "apache-2.0"

#: Git blob OIDs (sha1 of "blob <size>\\0" + bytes) of small files at BASE_REVISION.
FILE_OIDS: Final = {
    "tokenizer.json": "443909a61d429dff23010e5bddd28ff530edda00",
    "tokenizer_config.json": "acee076f49bf3c0298e15de0909d1da7b392f0c3",
    "vocab.json": "4783fe10ac3adce15ac8f358ef5462739852c569",
    "merges.txt": "20024bfe7c83998e9aeaf98a0cd6a2ce6306c2f0",
    "config.json": "1a2ea88621f4248fccfb575beaf64c1ea5d9587f",
    "generation_config.json": "b7849b3dad857bc1e227afd6ca21d1e4cb3f8f82",
}

SAFETENSORS_SHA256: Final = {
    "model-00001-of-00004.safetensors":
        "0b6f069918b07c064cbba8ae4f00f529aa9bbf84b7cdfcb7fc2694a40f6aa8ef",
    "model-00002-of-00004.safetensors":
        "c3d46733e7aa054ea7b063fbccd0a5a08446e7bd1814bef26936c5aa1331da62",
    "model-00003-of-00004.safetensors":
        "9fe45dacee087385b3d2d6dd27a7413a8a56d95f145772facc148fa86fc73446",
    "model-00004-of-00004.safetensors":
        "5aa6e5cbe642377fd441fb4e60e83cca96b2bcd9820e245b9ea06d94653f17f2",
}
CHAT_TEMPLATE_SHA256: Final = "cd8e9439f0570856fd70470bf8889ebd8b5d1107207f67a5efb46e342330527f"
EOS_TOKEN: Final = "<|im_end|>"  # noqa: S105
PAD_TOKEN: Final = "<|endoftext|>"  # noqa: S105

#: Reviewed official Runpod PyTorch image pinned by digest (torch 2.4.0, CUDA 12.4.1, Py 3.11).
POD_IMAGE: Final = (
    "runpod/pytorch@sha256:61a4aafb0094cd773f11eefa378929d5a687bd775febeb78eac62fc824141fb5"
)
POD_IMAGE_TAG: Final = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
POD_CUDA_VERSIONS: Final = ("12.4", "12.5", "12.6", "12.7", "12.8")
POD_LOCK: Final = "research/training/pod-requirements.lock"
