"""Fault injection via the Toxiproxy control API (stdlib only).

    python3 tools/toxic.py add      # inject slo.json -> teeth.latency_ms
    python3 tools/toxic.py remove
    python3 tools/toxic.py status

Used by `make prove-teeth`. The fault is deterministic and applied at the network
hop between the load generators and the API, so it is the same fault every run on
every machine -- which is what makes "both engines must fail" a real assertion
rather than a hope.
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slo as slo_mod  # noqa: E402

CTL = os.environ.get("TOXIPROXY_URL", "http://localhost:8474")
PROXY = "api"
TOXIC = "prove_teeth_latency"


def _call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{CTL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        raise SystemExit(
            f"cannot reach toxiproxy at {CTL} ({e.reason}). Is the stack up? "
            f"Try `make up`.")


def add():
    cfg = slo_mod.load()["teeth"]
    remove(quiet=True)
    status, body = _call("POST", f"/proxies/{PROXY}/toxics", {
        "name": TOXIC,
        "type": "latency",
        "stream": "downstream",
        "toxicity": 1.0,
        "attributes": {"latency": cfg["latency_ms"], "jitter": cfg["jitter_ms"]},
    })
    if status not in (200, 201):
        raise SystemExit(f"failed to add toxic: {status} {body}")
    print(f"injected {cfg['latency_ms']}ms +/-{cfg['jitter_ms']}ms latency "
          f"into every response")


def remove(quiet=False):
    status, _ = _call("DELETE", f"/proxies/{PROXY}/toxics/{TOXIC}")
    if not quiet:
        print("fault removed" if status in (204, 200)
              else "no fault was active")


def status_cmd():
    _, body = _call("GET", f"/proxies/{PROXY}")
    toxics = (body or {}).get("toxics", [])
    if not toxics:
        print("clean: no faults injected")
    else:
        for t in toxics:
            print(f"active: {t['name']} type={t['type']} attrs={t['attributes']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"add": add, "remove": remove, "status": status_cmd}.get(cmd, status_cmd)()
