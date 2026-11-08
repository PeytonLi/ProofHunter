"""Benign basic-tier probes, run as root before any source upload (PRD amendment A1).

Never contacts the provider gateway beyond a TCP connect from inside an empty network
namespace; the gateway SSH exposure is accepted residual risk and is not probed here.
"""

import ctypes
import json
import os
import socket
import subprocess
from pathlib import Path

ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}
PYTHON = "/usr/local/bin/python3"
DNS_NAMES = (
    "github.com",
    "pypi.org",
    "app.daytona.io",
    "api.render.com",
    "metadata.google.internal",
)
METADATA_TARGETS = ("169.254.169.254:80", "169.254.170.2:80")
CHILD_TARGETS = (
    "172.20.0.1:22",
    "140.82.112.3:443",
    "151.101.0.223:443",
    "1.1.1.1:443",
    "169.254.169.254:80",
    "169.254.170.2:80",
)
# Public build-time variables baked into the pinned python:3.12-slim image:
# GPG_KEY is the public fingerprint used to verify the CPython source tarball.
SAFE_ENV_NAMES = frozenset({"GPG_KEY"})
CREDENTIAL_PATHS = (
    "/root/.aws",
    "/root/.config/gcloud",
    "/root/.azure",
    "/root/.docker/config.json",
    "/root/.netrc",
    "/root/.git-credentials",
    "/root/.ssh",
    "/var/run/secrets",
    "/run/secrets",
)
CHILD = (
    "import json,os,socket\n"
    "out={'uid':os.getuid(),'groups':os.getgroups()}\n"
    "for t in json.loads(os.environ['PH_TARGETS']):\n"
    "    h,p=t.rsplit(':',1)\n"
    "    try:\n"
    "        socket.create_connection((h,int(p)),timeout=0.5).close();out[t]=False\n"
    "    except OSError:\n"
    "        out[t]=True\n"
    "print(json.dumps(out))\n"
)


def isolate() -> None:
    os.unshare(os.CLONE_NEWNET)
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    if ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) != 0:
        os._exit(1)


def metadata_denied(target: str) -> bool:
    """Denied unless a metadata service answers with a 2xx; provider 403 filters count as denied."""
    host, port = target.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=0.5) as conn:
            conn.settimeout(1)
            request = b"GET / HTTP/1.0\r\nHost: " + host.encode() + b"\r\nMetadata: true\r\n\r\n"
            conn.sendall(request)
            head = conn.recv(64)
    except OSError:
        return True
    parts = head.split(b" ", 2)
    return not (len(parts) >= 2 and parts[1][:1] == b"2")


def dns_denied(name: str) -> bool:
    try:
        socket.getaddrinfo(name, 443)
    except OSError:
        return True
    return False


def main() -> None:
    secrets_absent = not any(
        key.upper().endswith(("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIALS"))
        and key not in SAFE_ENV_NAMES
        for key in os.environ
    ) and not any(Path(path).exists() for path in CREDENTIAL_PATHS)
    os.environ.clear()
    os.environ.update(ENV)
    probes = {f"dns:{name}": dns_denied(name) for name in DNS_NAMES}
    probes.update({f"metadata:{t}": metadata_denied(t) for t in METADATA_TARGETS})
    child_ok = False
    try:
        child = subprocess.run(  # noqa: S603
            [PYTHON, "-I", "-B", "-c", CHILD],
            env={**ENV, "PH_TARGETS": json.dumps(CHILD_TARGETS)},
            cwd="/",
            timeout=10,
            capture_output=True,
            check=False,
            preexec_fn=isolate,
        )
        result = json.loads(child.stdout) if child.returncode == 0 else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        result = {}
    if type(result) is dict:
        child_ok = result.get("uid") == 65534 and result.get("groups") == []
        probes.update({f"child:{t}": result.get(t) is True for t in CHILD_TARGETS})
    else:
        probes.update({f"child:{t}": False for t in CHILD_TARGETS})
    print(
        json.dumps(
            {
                "egress_policy": "restricted",
                "dns_denied": all(probes[f"dns:{n}"] for n in DNS_NAMES),
                "metadata_denied": all(probes[f"metadata:{t}"] for t in METADATA_TARGETS),
                "secrets_absent": secrets_absent,
                "restricted_child": child_ok,
                "child_network_isolated": child_ok
                and all(probes[f"child:{t}"] for t in CHILD_TARGETS),
                "probes_denied": probes,
            }
        )
    )


if __name__ == "__main__":
    main()
