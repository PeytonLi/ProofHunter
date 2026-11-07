"""Benign operator-only network probes, before any source upload."""

import json
import os
import socket
import subprocess

ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}


def denied(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return False
    except OSError:
        return True


def main() -> None:
    secrets_absent = not any(
        key.endswith(("KEY", "TOKEN", "SECRET", "PASSWORD")) for key in os.environ
    )
    os.environ.clear()
    os.environ.update(ENV)
    # The snapshot pins a controlled operator-owned endpoint in this root-owned file.
    with open("/opt/proofhunter/canary-host", encoding="ascii") as stream:
        controlled_host = stream.read(254).strip()
    try:
        socket.getaddrinfo(controlled_host, 443)
        dns_denied = False
    except socket.gaierror:
        dns_denied = True
    egress = dns_denied and all(
        denied(host, 443)
        for host in (
            controlled_host,
            "1.1.1.1",
            "github.com",
            "pypi.org",
            "app.daytona.io",
        )
    )
    metadata = all(denied(host, 80) for host in ("169.254.169.254", "169.254.170.2"))
    child = subprocess.run(
        [
            "/usr/bin/setpriv",
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
            "--no-new-privs",
            "/usr/bin/python3",
            "-I",
            "-c",
            "import os;assert os.getuid()==65534;assert os.getgroups()==[]",
        ],
        env=ENV,
        cwd="/",
        timeout=1,
        capture_output=True,
        check=False,
    )
    print(
        json.dumps(
            {
                "egress_denied": egress,
                "metadata_denied": metadata,
                "secrets_absent": secrets_absent,
                "restricted_child": child.returncode == 0,
            }
        )
    )


if __name__ == "__main__":
    main()
