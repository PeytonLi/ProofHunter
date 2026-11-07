"""Download the pinned base snapshot and verify every weight/tokenizer file hash."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> int:
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    pins = json.loads(Path(sys.argv[1]).read_text())
    path = Path(snapshot_download(pins["repo"], revision=pins["revision"], local_dir=sys.argv[2],
                                  allow_patterns=[*pins["sha256"], *pins["oids"],
                                                  "model.safetensors.index.json"]))
    bad = []
    for name, want in pins["sha256"].items():
        h = hashlib.sha256()
        with (path / name).open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            bad.append(name)
    for name, want in pins["oids"].items():
        data = (path / name).read_bytes()
        if hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest() != want:
            bad.append(name)
    print(json.dumps({"verified": not bad, "bad": bad}))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
