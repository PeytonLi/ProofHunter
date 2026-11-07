"""Trusted root-owned snapshot entry point. Never run this on the control plane."""

import contextlib
import ctypes
import hashlib
import json
import os
import resource
import selectors
import signal
import subprocess
import time
from pathlib import Path

ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}
CHILD = (
    "import sys,json;sys.path.insert(0,'/tmp/proofhunter-target');"
    "from checkout import total_cents;d=json.load(sys.stdin);"
    "v=total_cents(d['prices_cents'],d['discount_percent']);"
    "print(json.dumps({'value':v},allow_nan=False))"
)


def restrict(memory_mib: int, processes: int, seconds: int) -> None:
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    if ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) != 0:
        os._exit(1)
    for kind, limit in (
        (resource.RLIMIT_AS, memory_mib * 1024 * 1024),
        (resource.RLIMIT_CPU, seconds),
        (resource.RLIMIT_NPROC, processes),
        (resource.RLIMIT_NOFILE, 32),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(kind, (limit, limit))


def execute(data: dict[str, object]) -> tuple[str, int | None, bool]:
    policy = data["resources"]
    if not isinstance(policy, dict):
        raise ValueError("policy")
    seconds = policy["execution_timeout_seconds"]
    memory = policy["memory_mib"]
    processes = policy["max_processes"]
    cap = policy["max_output_bytes"]
    for value, low, high in (
        (seconds, 1, 5),
        (memory, 64, 1024),
        (processes, 1, 64),
        (cap, 1, 16384),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("policy")
    source = Path("/var/proofhunter/checkout.py").read_bytes()
    if len(source) > 65536:
        raise ValueError("source")
    target = Path("/tmp/proofhunter-target")  # noqa: S108
    target.mkdir(mode=0o755)
    (target / "checkout.py").write_bytes(source)
    (target / "checkout.py").chmod(0o444)
    process = subprocess.Popen(  # noqa: S603
        ["/usr/bin/python3", "-I", "-B", "-c", CHILD],
        cwd=str(target),
        env=ENV,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=lambda: restrict(memory, processes, seconds),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = bytearray()
    count = 0
    deadline = time.monotonic() + seconds
    try:
        process.stdin.write(json.dumps(data["input"]).encode())
        process.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None, False
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, min(4096, cap + 1 - count))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                count += len(chunk)
                if count > cap:
                    return "invalid", None, True
                if key.data == "stdout":
                    output.extend(chunk)
        try:
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return "timeout", None, False
        if code != 0:
            return "nonzero", None, False
        parsed = json.loads(output, object_pairs_hook=unique_keys)
        if (
            type(parsed) is dict
            and set(parsed) == {"value"}
            and type(parsed["value"]) is int
            and -(2**63) <= parsed["value"] < 2**63
        ):
            return "ok", parsed["value"], False
        return "invalid", None, False
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        if not process.stdin.closed:
            process.stdin.close()
        selector.close()
        process.stdout.close()
        process.stderr.close()


def unique_keys(items: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def main() -> None:
    os.environ.clear()
    os.environ.update(ENV)
    path = Path("/var/proofhunter/request.json")
    os.chown(path, 0, 0)
    path.chmod(0o600)
    raw = path.read_bytes()
    if len(raw) > 8192:
        raise ValueError("request")
    data = json.loads(raw, object_pairs_hook=unique_keys)
    try:
        source_hash = hashlib.sha256(Path("/var/proofhunter/checkout.py").read_bytes()).hexdigest()
        input_hash = hashlib.sha256(
            json.dumps(
                data["input"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if (
            source_hash != data["binding"]["source_sha256"]
            or input_hash != data["binding"]["input_sha256"]
        ):
            raise ValueError("binding")
        outcome, value, truncated = execute(data)
    except Exception:
        outcome, value, truncated = "invalid", None, False
    print(
        json.dumps(
            {
                "binding": data["binding"],
                "outcome": outcome,
                "value": value,
                "truncated": truncated,
            },
            separators=(",", ":"),
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
