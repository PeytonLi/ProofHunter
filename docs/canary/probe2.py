"""Classification probe: is the metadata 403 an egress filter or the real service? Benign only."""
import json, os, socket, ssl, struct, subprocess
def raw_http(host, port=80, path="/", hdr=None, n=600):
    try:
        s = socket.create_connection((host, port), timeout=3); s.settimeout(3)
        h = "".join(f"{k}: {v}\r\n" for k, v in (hdr or {}).items())
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{h}Connection: close\r\n\r\n".encode())
        buf = b""
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c: break
            buf += c
        s.close(); return buf.decode("latin1")
    except Exception as e:
        return "ERR:" + type(e).__name__ + ":" + str(e)[:60]
def tls(ip, sni):
    try:
        raw = socket.create_connection((ip, 443), timeout=4)
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        s = ctx.wrap_socket(raw, server_hostname=sni); s.settimeout(4)
        s.sendall(f"HEAD / HTTP/1.1\r\nHost: {sni}\r\nConnection: close\r\n\r\n".encode())
        d = s.recv(120); v = s.version(); s.close()
        return {"handshake": True, "version": v, "status": d.split(b"\r\n")[0].decode("latin1")}
    except Exception as e:
        return {"handshake": False, "err": type(e).__name__, "detail": str(e)[:80]}
def tcp(host, port):
    try:
        s = socket.create_connection((host, port), timeout=3); s.settimeout(2)
        try:
            d = s.recv(64)
        except Exception as e:
            d = ("recv:" + type(e).__name__).encode()
        s.close(); return {"connect": True, "first_bytes": d.decode("latin1")[:64]}
    except Exception as e:
        return {"connect": False, "err": type(e).__name__}
def udp_dns(server):
    q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + b"".join(bytes([len(p)]) + p.encode() for p in "example.com".split(".")) + b"\x00\x00\x01\x00\x01"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3); s.sendto(q, (server, 53))
        d, _ = s.recvfrom(512); s.close(); return {"reply": True, "bytes": len(d)}
    except Exception as e:
        return {"reply": False, "err": type(e).__name__}
out = {}
out["metadata_full"] = raw_http("169.254.169.254", 80, "/latest/meta-data/")
out["testnet_203.0.113.1:80"] = raw_http("203.0.113.1", 80, "/")
out["testnet_198.51.100.7:80"] = raw_http("198.51.100.7", 80, "/")
out["one.one.one.one:80_by_ip"] = raw_http("1.1.1.1", 80, "/")
out["tls"] = {"1.1.1.1/one.one.one.one": tls("1.1.1.1", "one.one.one.one"), "140.82.112.3/github.com": tls("140.82.112.3", "github.com"), "151.101.0.223/pypi.org": tls("151.101.0.223", "pypi.org"), "169.254.169.254:443": tls("169.254.169.254", "metadata")}
out["tcp_odd_ports"] = {"1.1.1.1:12345": tcp("1.1.1.1", 12345), "203.0.113.1:22": tcp("203.0.113.1", 22), "172.20.0.1:22": tcp("172.20.0.1", 22)}
out["udp_dns"] = {s: udp_dns(s) for s in ("8.8.8.8", "1.1.1.1", "172.20.0.1")}
try:
    out["ipv6_metadata"] = tcp("fd00:ec2::254", 80)
except Exception as e:
    out["ipv6_metadata"] = str(e)
def walk(p):
    res = []
    for root, dirs, files in os.walk(p):
        for f in files:
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp); res.append({"path": fp, "size": st.st_size, "mode": oct(st.st_mode)})
            except Exception as e:
                res.append({"path": fp, "err": type(e).__name__})
        for dname in dirs: res.append({"dir": os.path.join(root, dname)})
    return res
out["root_daytona"] = walk("/root/.daytona") if os.path.isdir("/root/.daytona") else ("file" if os.path.exists("/root/.daytona") else "absent")
out["setpriv_nobody"] = subprocess.run(["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs", "/usr/local/bin/python3", "-I", "-c", "import os;print(os.getuid(),os.getgroups())"], capture_output=True, text=True, timeout=10).stdout.strip()
print(json.dumps(out))
