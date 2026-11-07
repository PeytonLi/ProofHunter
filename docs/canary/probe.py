"""Benign in-sandbox probe. Prints one JSON object. Application-level evidence only."""
import json, os, socket, ssl, time, glob, subprocess, resource

def dns(host):
    try:
        return {"resolved": True, "addrs": sorted({a[4][0] for a in socket.getaddrinfo(host, 443)})[:4]}
    except Exception as e:
        return {"resolved": False, "err": type(e).__name__}

def tcp(host, port, timeout=3):
    t = time.monotonic()
    try:
        s = socket.create_connection((host, port), timeout=timeout); s.close()
        return {"connect": True, "ms": int((time.monotonic()-t)*1000)}
    except Exception as e:
        return {"connect": False, "err": type(e).__name__}

def http(host, port=80, path="/", headers=None, timeout=3):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        h = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{h}Connection: close\r\n\r\n".encode())
        data = s.recv(200); s.close()
        line = data.split(b"\r\n", 1)[0].decode("latin1")[:60]
        return {"status_line": line or None, "bytes": len(data)}
    except Exception as e:
        return {"status_line": None, "err": type(e).__name__}

def https(host, path="/", timeout=4):
    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
        ctx = ssl.create_default_context()
        s = ctx.wrap_socket(raw, server_hostname=host); s.settimeout(timeout)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: ph-canary\r\nConnection: close\r\n\r\n".encode())
        data = s.recv(200); s.close()
        return {"tls": True, "status_line": data.split(b"\r\n", 1)[0].decode("latin1")[:60]}
    except Exception as e:
        return {"tls": False, "err": type(e).__name__, "detail": str(e)[:80]}

def read(path, n=200):
    try:
        with open(path) as f: return f.read(n).strip()
    except Exception as e:
        return "ERR:" + type(e).__name__

out = {"uid": os.getuid(), "user_env_keys": sorted(os.environ.keys())}
cred_like = [k for k in os.environ if any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASS", "AUTH", "CRED", "DAYTONA", "RENDER", "GITHUB", "AWS"))]
out["credential_like_env_keys"] = cred_like
out["credential_files"] = [p for pat in ("/var/run/secrets/**", "/run/secrets/**", "/root/.aws/*", "/root/.config/gcloud/*", "/root/.docker/config.json", "/home/*/.aws/*", "/root/.netrc", "/home/*/.netrc", "/root/.git-credentials", "/home/*/.git-credentials", "/etc/daytona*", "/root/.daytona*", "/home/*/.daytona*") for p in glob.glob(pat, recursive=True)]
out["resolv_conf"] = read("/etc/resolv.conf", 300)
try:
    out["default_route"] = [l.split() for l in open("/proc/net/route").read().splitlines()[1:3]]
except Exception as e:
    out["default_route"] = str(e)
out["dns"] = {h: dns(h) for h in ("example.com", "github.com", "pypi.org", "proofhunter-core", "api.render.com", "metadata.google.internal", "app.daytona.io")}
out["https"] = {h: https(h) for h in ("example.com", "github.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "api.render.com", "app.daytona.io")}
out["http"] = {"example.com": http("example.com")}
out["raw_ip_tcp"] = {"1.1.1.1:443": tcp("1.1.1.1", 443), "8.8.8.8:53": tcp("8.8.8.8", 53)}
out["metadata"] = {
    "169.254.169.254:80 aws": http("169.254.169.254", 80, "/latest/meta-data/"),
    "169.254.169.254:80 gcp": http("169.254.169.254", 80, "/computeMetadata/v1/", {"Metadata-Flavor": "Google"}),
    "169.254.169.254:80 azure": http("169.254.169.254", 80, "/metadata/instance?api-version=2021-02-01", {"Metadata": "true"}),
    "169.254.170.2:80 ecs": http("169.254.170.2", 80, "/v2/metadata"),
    "metadata.google.internal:80": http("metadata.google.internal", 80, "/computeMetadata/v1/", {"Metadata-Flavor": "Google"}),
    "100.100.100.200:80 alibaba": http("100.100.100.200", 80, "/latest/meta-data/"),
}
out["internal"] = {
    "proofhunter-core:10000": tcp("proofhunter-core", 10000),
    "10.0.0.1:80": tcp("10.0.0.1", 80, 2),
    "172.17.0.1:80": tcp("172.17.0.1", 80, 2),
    "192.168.0.1:80": tcp("192.168.0.1", 80, 2),
}
gw = None
try:
    for l in open("/proc/net/route").read().splitlines()[1:]:
        f = l.split()
        if f[1] == "00000000":
            gw = socket.inet_ntoa(bytes.fromhex(f[2])[::-1]); break
except Exception:
    pass
out["gateway"] = gw
if gw:
    out["internal"].update({f"{gw}:{p}": tcp(gw, p, 2) for p in (22, 80, 443, 2280, 8080, 10000)})
out["cgroup"] = {k: read(p) for k, p in {
    "cpu.max": "/sys/fs/cgroup/cpu.max", "memory.max": "/sys/fs/cgroup/memory.max",
    "pids.max": "/sys/fs/cgroup/pids.max", "memory.limit_in_bytes(v1)": "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    "cpu.cfs_quota_us(v1)": "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"}.items()}
out["nproc"] = os.cpu_count()
out["sched_affinity"] = len(os.sched_getaffinity(0))
out["meminfo_total_kb"] = read("/proc/meminfo", 60)
# memory: try to allocate 1.5 GiB in a child; report outcome.
r = subprocess.run(["python3", "-c", "b=bytearray(1536*1024*1024);b[::4096]=b'x'*len(b[::4096]);print('allocated')"], capture_output=True, timeout=60)
out["alloc_1536MiB"] = {"rc": r.returncode, "out": r.stdout.decode()[:20], "err_tail": r.stderr.decode()[-80:]}
r = subprocess.run(["python3", "-c", "b=bytearray(512*1024*1024);b[::4096]=b'x'*len(b[::4096]);print('allocated')"], capture_output=True, timeout=60)
out["alloc_512MiB"] = {"rc": r.returncode, "out": r.stdout.decode()[:20]}
# cpu: 2 busy procs for 2s each; compare wall vs cpu to see throttling.
t = time.monotonic()
ps = [subprocess.Popen(["python3", "-c", "import time\nt=time.process_time()\nwhile time.process_time()-t<2: pass"]) for _ in range(2)]
for p in ps: p.wait()
out["cpu_2x2s_wall_s"] = round(time.monotonic() - t, 2)
out["tools"] = {b: os.path.exists(b) for b in ("/usr/bin/python3", "/usr/bin/setpriv", "/usr/bin/sudo", "/usr/local/bin/python3")}
print(json.dumps(out))
