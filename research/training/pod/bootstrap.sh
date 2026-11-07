#!/usr/bin/env bash
# Pod setup (idempotent): deadman, idle shutdown, hash-locked deps, pinned + verified base model.
set -euo pipefail
DEADMAN_SECONDS="${1:?deadman seconds}"
IDLE_MINUTES="${2:?idle minutes}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT=/workspace/ph
mkdir -p "$ROOT"
touch "$ROOT/keepalive"
stop_pod() { runpodctl remove pod "${RUNPOD_POD_ID}" || kill -TERM 1; }
if [ ! -f "$ROOT/.guards" ]; then
  export -f stop_pod
  nohup bash -c "sleep ${DEADMAN_SECONDS}; stop_pod" >"$ROOT/deadman.log" 2>&1 &
  nohup bash -c "
    while sleep 60; do
      if pgrep -f 'hf_generate.py|train_qlora.py|fetch_model.py|pip install' >/dev/null; then
        touch $ROOT/keepalive; continue; fi
      age=\$(( \$(date +%s) - \$(stat -c %Y $ROOT/keepalive) ))
      if [ \"\$age\" -gt $(( IDLE_MINUTES * 60 )) ]; then echo idle \$age; stop_pod; fi
    done" >"$ROOT/idle.log" 2>&1 &
  touch "$ROOT/.guards"
fi
if [ ! -f "$ROOT/.bootstrapped" ]; then
  python -m pip install --quiet --no-deps --require-hashes -r "$HERE/pod-requirements.lock"
  python "$HERE/fetch_model.py" "$HERE/model_pins.json" /workspace/model
  touch "$ROOT/.bootstrapped"
fi
echo "runpodctl=$(command -v runpodctl || echo missing) pod_key=$([ -n "${RUNPOD_API_KEY:-}" ] && echo set || echo missing)"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
