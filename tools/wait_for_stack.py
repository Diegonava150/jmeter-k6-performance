"""Block until the system under test actually serves the workload's first path.

Not a port check. A port opens long before PostgREST has finished reading the
schema cache, and a load test that starts against a half-warm service produces
numbers that are real but meaningless.
"""
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slo as slo_mod  # noqa: E402

TIMEOUT_S = int(os.environ.get("STACK_TIMEOUT_S", "120"))


def main():
    cfg = slo_mod.load()
    url = f"{cfg['base_url']}{cfg['scenarios'][0]['path']}"
    deadline = time.time() + TIMEOUT_S
    last = "no attempt made"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    waited = TIMEOUT_S - int(deadline - time.time())
                    print(f"stack ready after ~{waited}s -> {url}")
                    return 0
                last = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:  # connection refused, DNS, reset
            last = type(e).__name__
        time.sleep(2)
    print(f"stack did not become ready within {TIMEOUT_S}s (last: {last})",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
