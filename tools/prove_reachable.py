"""Prove every threshold is capable of firing.

`make prove-teeth` shows the SLO *as a whole* can fail, by breaking the service
and watching the verdict flip. That is necessary and it is not sufficient: a
verdict can go red on one threshold while another one in the same block is
arithmetically incapable of ever firing, and nothing in a passing or failing run
will tell you which.

That is not hypothetical. This repo shipped exactly that bug. The closed profile
carried `min_rps: 15` alongside `p95_ms: 400`, and in a closed model throughput
is

    rps = vus / (think_time + latency)

which is a property of the LOAD GENERATOR, not the service. Solving for the
latency at which 20 VUs with 300ms think time finally drop below 15/s gives
1033ms -- so p95 had already failed by 2.6x before throughput could breach.
`min_rps` was decoration. It never fired in any run, including under a 600ms
injected fault, and it never could have.

This checker is analytical rather than empirical on purpose: it needs no Docker,
no load generator and no runner time, it is deterministic, and it fails the
build the moment someone adds a threshold that cannot fire.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slo as slo_mod  # noqa: E402

BAR = "-" * 74


def closed_max_rps(profile, latency_ms):
    """Closed model: each VU sends, waits for the response, then thinks."""
    cycle_s = (profile["think_time_ms"] + latency_ms) / 1000.0
    return profile["vus"] / cycle_s if cycle_s > 0 else float("inf")


def open_max_rps(profile, latency_ms):
    """Open model: offered rate, capped by how much concurrency the fleet has.

    Little's Law -- sustaining R requests/s at L seconds latency needs R*L
    requests in flight. Once that exceeds max_vus the client cannot keep up and
    achieved throughput falls to max_vus/L, independently of the target.
    """
    latency_s = latency_ms / 1000.0
    if latency_s <= 0:
        return profile["rate_per_s"]
    return min(profile["rate_per_s"], profile["max_vus"] / latency_s)


MODELS = {"closed": closed_max_rps, "open": open_max_rps}


def check_profile(name, profile):
    """Return a list of problems for one profile."""
    slo = profile["slo"]
    model = profile["model"]
    max_rps = MODELS[model]
    problems = []

    print(f"  profile '{name}' ({model} model)")

    lat_gate = slo.get("p95_ms")
    print(f"    latency gate       p95 > {lat_gate}ms")

    if "min_rps" not in slo:
        print("    throughput gate    (none declared)")
        if model == "open":
            problems.append(
                f"profile '{name}' is an open model with no min_rps. Throughput is an "
                f"independent signal here and declaring no threshold on it wastes the "
                f"only thing this profile measures that the closed one cannot.")
        else:
            print("      -> correct for a closed model: throughput is generator-bound")
        print()
        return problems

    min_rps = slo["min_rps"]
    print(f"    throughput gate    rps < {min_rps}/s")

    # Walk latency upward and find the first point each gate trips.
    lat_breach_ms = None
    rps_breach_ms = None
    ms = 1
    while ms <= 60_000:
        if rps_breach_ms is None and max_rps(profile, ms) < min_rps:
            rps_breach_ms = ms
        if lat_breach_ms is None and ms > lat_gate:
            lat_breach_ms = ms
        if rps_breach_ms is not None and lat_breach_ms is not None:
            break
        ms += 1

    if rps_breach_ms is None:
        problems.append(
            f"profile '{name}': min_rps={min_rps}/s can never fire. Even at 60s "
            f"latency this model still delivers "
            f"{max_rps(profile, 60_000):.1f}/s.")
        print(f"      -> UNREACHABLE at any latency\n")
        return problems

    print(f"      -> throughput first breaches at ~{rps_breach_ms}ms latency")
    print(f"      -> latency    first breaches at ~{lat_breach_ms}ms latency")

    if rps_breach_ms > lat_gate:
        ratio = rps_breach_ms / lat_gate
        problems.append(
            f"profile '{name}': min_rps={min_rps}/s is dominated by p95={lat_gate}ms. "
            f"Throughput cannot breach until latency reaches {rps_breach_ms}ms, by which "
            f"point p95 has already failed by {ratio:.1f}x. The threshold cannot fire "
            f"on its own and is decoration.")
        print(f"      -> DOMINATED by the latency gate ({ratio:.1f}x)\n")
    else:
        print(f"      -> reachable: throughput can breach before latency does\n")

    return problems


# The config this repo actually shipped, kept as a regression fixture. If the
# checker ever stops rejecting it, the checker is broken.
HISTORICAL_BUG = {
    "model": "closed", "vus": 20, "ramp_up_s": 10, "duration_s": 60,
    "think_time_ms": 300,
    "slo": {"p95_ms": 400, "p99_ms": 900, "error_rate": 0.01, "min_rps": 15},
}


def _self_test():
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        problems = check_profile("historical", HISTORICAL_BUG)
    assert problems, "checker failed to reject the config that shipped the bug"
    msg = problems[0]
    assert "dominated" in msg, msg
    # 1034 is the first breaching whole millisecond; 20/(0.3+1.033) == 15.0 exactly.
    assert "1034ms" in msg, f"expected the 1034ms breach point, got: {msg}"
    assert "2.6x" in msg, f"expected the 2.6x domination factor, got: {msg}"
    print("self-test OK")
    print("  the config this repo shipped is correctly rejected:")
    print(f"    {msg}")
    return 0


def main():
    if "--self-test" in sys.argv:
        return _self_test()
    cfg = slo_mod.load()
    print(BAR)
    print("  THRESHOLD REACHABILITY   every gate must be capable of firing")
    print(BAR)

    problems = []
    for name, profile in cfg["profiles"].items():
        problems.extend(check_profile(name, profile))

    if problems:
        print("  RESULT: FAIL")
        for p in problems:
            print(f"    x {p}")
        print(BAR)
        return 1

    print("  RESULT: PASS -- every declared threshold can fire before, or "
          "independently of,")
    print("          the others in its profile.")
    print(BAR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
