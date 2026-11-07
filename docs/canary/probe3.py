import json, subprocess
def run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        return {"rc": r.returncode, "out": r.stdout.strip()[-300:], "err": r.stderr.strip()[-200:]}
    except Exception as e:
        return {"err": type(e).__name__}
T = ("import socket,json,os\nres={'uid':os.getuid(),'ifaces':sorted(os.listdir('/sys/class/net'))}\n"
     "for h,p in (('172.20.0.1',22),('140.82.112.3',443),('169.254.169.254',80)):\n"
     "    try:\n        socket.create_connection((h,p),timeout=2).close();res[f'{h}:{p}']='connected'\n"
     "    except Exception as e: res[f'{h}:{p}']=type(e).__name__\n"
     "print(json.dumps(res))")
PY = "/usr/local/bin/python3"
out = {
  "capeff": open("/proc/self/status").read().split("CapEff:")[1].split()[0],
  "unshare_bin": run(["/usr/bin/which", "unshare"]),
  "baseline": run([PY, "-I", "-c", T]),
  "unshare_n_root": run(["/usr/bin/unshare", "--net", "--", PY, "-I", "-c", T]),
  "unshare_n_then_setpriv": run(["/usr/bin/unshare", "--net", "--", "/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs", PY, "-I", "-c", T]),
  "userns_unpriv": run(["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/unshare", "-Urn", "--", PY, "-I", "-c", T]),
}
print(json.dumps(out))
