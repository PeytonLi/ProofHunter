#!/usr/bin/env bash
# Pod setup: deadman, hash-locked deps, pinned + verified base model.
set -euo pipefail
DEADMAN_SECONDS="${1:?deadman seconds}"
cd /workspace/ph
# Deadman: stop the container no matter what the controller does.
nohup bash -c "sleep ${DEADMAN_SECONDS}; runpodctl remove pod \"\${RUNPOD_POD_ID}\" || kill -TERM 1" \
  >/workspace/ph/deadman.log 2>&1 &
python -m pip install --quiet --no-deps --require-hashes -r pod-requirements.lock
python fetch_model.py model_pins.json /workspace/model
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
