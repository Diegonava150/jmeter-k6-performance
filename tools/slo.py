"""Loader for slo.json -- the single source of truth.

Every other tool, and both engines, get their numbers from here. There is no
second place to change a threshold.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLO_PATH = os.path.join(ROOT, "slo.json")

DEFAULT_PROFILE = "closed"


def load(path=SLO_PATH):
    with open(path) as fh:
        return json.load(fh)


def profile(cfg, name=None):
    """One workload profile, by name. Unknown names fail loudly rather than
    silently falling back -- a typo'd profile that quietly ran the default would
    produce a perfectly plausible report of the wrong thing."""
    name = name or os.environ.get("PROFILE") or DEFAULT_PROFILE
    try:
        return name, cfg["profiles"][name]
    except KeyError:
        raise SystemExit(
            f"unknown profile {name!r}; slo.json defines "
            f"{sorted(cfg['profiles'])}")


def steady_window_ms(prof):
    """Ramp length in ms. Both engines discard this window, measured from the
    first request either of them sent."""
    return prof["ramp_up_s"] * 1000


def scenarios(cfg):
    """Scenarios with a cumulative weight band, so both engines pick the same way."""
    out, running = [], 0
    for s in cfg["scenarios"]:
        running += s["weight"]
        out.append({**s, "cum": running})
    if running != 100:
        raise SystemExit(f"scenario weights must total 100, got {running}")
    return out


if __name__ == "__main__":
    # Shell-friendly accessor:
    #   python3 tools/slo.py profiles.closed.vus   -> 20
    import sys
    cur = load()
    for part in sys.argv[1].split("."):
        cur = cur[part]
    print(cur)
