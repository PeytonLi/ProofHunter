# ProofHunter Daytona basic-tier canary report

Date: 2026-11-07 (about 23:40 UTC). Account: the shared basic Daytona org (Tier 1/2, same key ForkSafe uses). No org-level setting was read or changed. No ProofHunter code, PR source or secrets went into any sandbox.

## What ran

- One new ProofHunter-only snapshot `proofhunter-py312-slim-4840d514c583`, made from `python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54`, 1 CPU, 1 GiB, 3 GiB disk, entrypoint `sleep infinity`. It took 15 seconds.
- Two short canary sandboxes from that snapshot with `public=False`, `network_block_all=True`, `env_vars={}`, `secrets={}`, `volumes=[]`, `ephemeral=True`, `auto_stop_interval=1`, `auto_delete_interval=0`, `ttl_minutes=5`. Each ran one benign Python probe script that was uploaded, then got deleted.

## Results

| Check | What I saw | Verdict |
|---|---|---|
| DNS | Every name lookup failed (example.com, github.com, pypi.org, api.render.com, app.daytona.io, proofhunter-core, metadata.google.internal). UDP 53 to 8.8.8.8, 1.1.1.1 and the gateway timed out. TCP 53 to 8.8.8.8 was refused. | Blocked |
| Plain HTTP (port 80), any IP | An envoy proxy answers every port-80 request with `403 Forbidden`, body "Internet is restricted on Tier 1 and Tier 2." This includes unroutable test addresses 203.0.113.1 and 198.51.100.7, so the 403 is the filter and no real server is reached | Blocked |
| HTTPS by IP | github.com (140.82.112.3) and pypi.org (151.101.0.223) completed TLS 1.3 and returned `HTTP/1.1 200 OK`. 1.1.1.1:443 and 169.254.169.254:443 got a connection reset | Restricted, not blocked. Daytona's "essential services" (GitHub, PyPI) stay reachable, as Daytona documents for Tier 1/2 |
| Other TCP ports | 1.1.1.1:12345 and 203.0.113.1:22 were refused | Blocked |
| Cloud metadata 169.254.169.254, 169.254.170.2, 100.100.100.200 | Port 80 gave the same envoy 403 "Internet is restricted" text as the unroutable test IPs. Port 443 was reset. IPv6 fd00:ec2::254 had no route. No metadata service answered | Denied |
| ProofHunter / Render | `proofhunter-core` does not resolve, and the Render private network is not reachable. api.render.com does not resolve | Not reachable |
| **Sandbox gateway 172.20.0.1** | **Port 22 answers with an SSH banner `SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.20`. That is a provider host service (the Daytona runner host or its network) reachable from inside the sandbox. Ports 2280, 8080 and 10000 were refused. I did not try to log in or probe it further** | **Provider-internal endpoint reachable. HARD STOP trigger** |
| Credentials | Sandbox env has only Daytona metadata variables (sandbox id, org id, region, snapshot name, OTEL endpoint) plus the image's own PYTHON_*/GPG_KEY values. GPG_KEY is the public Python release signing key fingerprint from the image, not a secret. No key, token, password or cloud credential appeared. `/root/.daytona` holds only the entrypoint session files (an 18-byte cmd.sh and empty pipes). No /run/secrets, .aws, .netrc, .git-credentials or docker config | No credentials found |
| User | Commands ran as uid 0 in the container, because the image has no `daytona` user. `setpriv` to 65534 with no groups worked | Root inside the container. The wrapper must drop privileges itself |
| CPU | cgroup `cpu.max` = `100000 100000` (1 CPU). Two busy 2-second processes took 4.02 s wall, so CPU is throttled to 1 core. `nproc` still shows the host's 48 | Enforced (1 CPU) |
| Memory | cgroup `memory.max` = 1 GiB. A 1.5 GiB allocation was OOM-killed (SIGKILL), and 512 MiB worked. `pids.max` = 16384 | Enforced (1 GiB) |
| Image pinning | The snapshot API kept the exact `@sha256:` reference as `image_name`, and the internal ref is `cr.app.daytona.io/sbox/daytona-4840d514...:daytona` (named by the digest) | Pinned by digest, verifiable through the API |
| Timeout | `process.exec("sleep 30", timeout=3)` raised `DaytonaProcessExecutionTimeoutError` after 3.1 s | Enforced |
| Deletion | `delete(wait=True)` returned in about 0.8 s. A label list right after showed the sandbox in DESTROYING, and it was gone 3.2 s later. `get(id)` returned NotFound. No ProofHunter-labelled sandboxes are left | Works. Verify absence by polling |

## Leftovers and spend

- Sandboxes: none left. Snapshot `proofhunter-py312-slim-4840d514c583` was kept (about 40 MB, ProofHunter-only) for the runner. Delete it if the plan is dropped.
- Spend: two 1 vCPU / 1 GiB sandboxes for about 2 minutes total, well under $0.05.

## Follow-up: empty network namespace for the untrusted child (canary 3)

Peyton accepted the gateway SSH exposure as residual risk and asked for a no-network child as an extra control.
`os.unshare`/`unshare --net` works as root inside the sandbox (CapEff `000001ffffffffff`). Results:

- baseline: rc=0 {"uid": 0, "ifaces": ["eth0", "lo"], "172.20.0.1:22": "connected", "140.82.112.3:443": "connected", "169.254.169.254:80": "connected"}
- unshare_n_root: rc=0 {"uid": 0, "ifaces": ["eth0", "lo"], "172.20.0.1:22": "OSError", "140.82.112.3:443": "OSError", "169.254.169.254:80": "OSError"}
- unshare_n_then_setpriv: rc=0 {"uid": 65534, "ifaces": ["eth0", "lo"], "172.20.0.1:22": "OSError", "140.82.112.3:443": "OSError", "169.254.169.254:80": "OSError"}
- userns_unpriv: rc=1 unshare: write failed /proc/self/uid_map: Operation not permitted

So a child started in a fresh network namespace and dropped to uid 65534 cannot reach the gateway SSH, GitHub or the metadata address. Unprivileged user namespaces are blocked, so the namespace must be created by the root wrapper before dropping privileges. The sandbox was deleted and confirmed gone on the first poll.
