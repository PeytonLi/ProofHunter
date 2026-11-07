"""Minimal Runpod REST client + pod session with deadman, teardown verification and ledger.

Only reviewed, repository-owned scripts are copied to the pod; no dataset code executes.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from research.generator import pins
from research.generator.ledger import JsonlLedger, gpu_entry
from research.schemas.models import SINGLE_JOB_CHECKIN_USD, CostStatus

API: Final = "https://rest.runpod.io/v1"
PRICING_REFERENCE: Final = "https://www.runpod.io/pricing (pod costPerHr from REST API)"
GPU_CHOICES: Final = {
    "rtx4090": ("NVIDIA GeForce RTX 4090", 0.89),
    "a6000": ("NVIDIA RTX A6000", 0.59),
    "a40": ("NVIDIA A40", 0.59),
}
SSH_DIR: Final = Path.home() / ".cache/proofhunter-research/ssh"


class RunpodError(RuntimeError):
    pass


class BudgetRefused(RuntimeError):
    pass


def estimate_usd(gpu: str, hours: float) -> float:
    return round(GPU_CHOICES[gpu][1] * hours, 4)


class RunpodClient:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("RUNPOD_API_KEY")
        if not key:
            raise RunpodError("RUNPOD_API_KEY is not set")
        self._key = key

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        req = urllib.request.Request(  # noqa: S310
            API + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                     "User-Agent": "proofhunter-research/1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https API)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RunpodError(f"{method} {path} -> HTTP {exc.code}") from None
        return json.loads(raw) if raw.strip() else {}

    def create_pod(self, body: dict[str, Any]) -> dict[str, Any]:
        out = self._call("POST", "/pods", body)
        if not isinstance(out, dict) or "id" not in out:
            raise RunpodError("pod creation returned no id")
        return out

    def get_pod(self, pod_id: str) -> dict[str, Any] | None:
        out = self._call("GET", f"/pods/{pod_id}")
        return out if isinstance(out, dict) and out.get("id") else None

    def list_pods(self) -> list[dict[str, Any]]:
        out = self._call("GET", "/pods")
        return list(out) if isinstance(out, list) else []

    def delete_pod(self, pod_id: str) -> None:
        self._call("DELETE", f"/pods/{pod_id}")


def ensure_key() -> Path:
    SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = SSH_DIR / "id_ed25519"
    if not key.exists():
        subprocess.run(["ssh-keygen"  # noqa: S603, S607
                       , "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                       check=True)
    return key


@dataclass
class PodSession:
    client: RunpodClient
    pod_id: str
    host: str
    port: int
    key: Path
    cost_per_hr: float
    started: float
    log: list[str] = field(default_factory=list)

    def _ssh_base(self) -> list[str]:
        return ["-i", str(self.key), "-o", "StrictHostKeyChecking=no", "-o",
                "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o",
                "ServerAliveInterval=30"]

    def run(self, command: str, timeout: float = 3600) -> str:
        proc = subprocess.run(["ssh",  # noqa: S603, S607
                               *self._ssh_base(), "-p", str(self.port),
                               f"root@{self.host}", command], capture_output=True, text=True,
                              timeout=timeout)
        self.log.append(f"$ {command}\n{proc.stdout[-4000:]}{proc.stderr[-4000:]}")
        if proc.returncode:
            raise RunpodError(f"remote command failed ({proc.returncode}): {command}\n"
                              f"{proc.stderr[-2000:]}")
        return proc.stdout

    def put(self, local: Path, remote: str) -> None:
        subprocess.run(["scp",  # noqa: S603, S607
                        *self._ssh_base(), "-P", str(self.port), "-r", str(local),
                        f"root@{self.host}:{remote}"], check=True, capture_output=True,
                       timeout=600)

    def get(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["scp",  # noqa: S603, S607
                        *self._ssh_base(), "-P", str(self.port), "-r",
                        f"root@{self.host}:{remote}", str(local)], check=True,
                       capture_output=True, timeout=1800)


def _wait_ready(client: RunpodClient, pod_id: str, timeout: float = 900) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pod = client.get_pod(pod_id)
        if pod and pod.get("publicIp") and (pod.get("portMappings") or {}).get("22"):
            return pod
        time.sleep(10)
    raise RunpodError("pod did not expose SSH in time")


def verify_teardown(client: RunpodClient, pod_id: str, tries: int = 12) -> bool:
    for _ in range(tries):
        if client.get_pod(pod_id) is None and all(p.get("id") != pod_id
                                                    for p in client.list_pods()):
            return True
        client.delete_pod(pod_id)
        time.sleep(5)
    return False


@contextmanager
def pod_session(*, run_id: str, gpu: str, max_hours: float, ledger: JsonlLedger,
                phase: str = "training", category: str = "training_gpu",
                client: RunpodClient | None = None) -> Iterator[PodSession]:
    """Create one pod; always delete it, verify deletion and record the ledger entry."""
    estimate = estimate_usd(gpu, max_hours)
    if estimate > SINGLE_JOB_CHECKIN_USD:
        raise BudgetRefused(f"job estimate ${estimate} exceeds the ${SINGLE_JOB_CHECKIN_USD} "
                            "check-in threshold; stop and report")
    client = client or RunpodClient()
    key = ensure_key()
    body = {
        "name": f"ph-{run_id}"[:60], "imageName": pins.POD_IMAGE, "cloudType": "SECURE",
        "computeType": "GPU", "gpuTypeIds": [GPU_CHOICES[gpu][0]], "gpuCount": 1,
        "containerDiskInGb": 80, "volumeInGb": 0, "ports": ["22/tcp"],
        "supportPublicIp": True, "allowedCudaVersions": list(pins.POD_CUDA_VERSIONS),
        "env": {"PUBLIC_KEY": key.with_suffix(".pub").read_text().strip()},
    }
    started = time.time()
    pod = client.create_pod(body)
    pod_id = str(pod["id"])
    cost_per_hr = float(pod.get("costPerHr") or GPU_CHOICES[gpu][1])
    timer = threading.Timer(max_hours * 3600 + 300, client.delete_pod, args=(pod_id,))
    timer.daemon = True
    timer.start()
    verified = False
    try:
        ready = _wait_ready(client, pod_id)
        session = PodSession(client, pod_id, str(ready["publicIp"]),
                             int(ready["portMappings"]["22"]), key, cost_per_hr, started)
        yield session
    finally:
        timer.cancel()
        try:
            client.delete_pod(pod_id)
        finally:
            verified = verify_teardown(client, pod_id)
            hours = (time.time() - started) / 3600
            ledger.append(gpu_entry(
                entry_id=f"{run_id}-pod", provider="runpod", resource_id=pod_id, run_id=run_id,
                hours=hours, unit_price=cost_per_hr, estimated_job_usd=estimate,
                pricing_reference=PRICING_REFERENCE, phase=phase, category=category,
                wall_ms=round(hours * 3_600_000, 1), teardown_verified=verified,
                cost_status=CostStatus.ESTIMATED))
        if not verified:
            raise RunpodError(f"teardown of pod {pod_id} could not be verified")


def remote_quote(text: str) -> str:
    return shlex.quote(text)
