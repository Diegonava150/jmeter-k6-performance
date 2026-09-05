"""The gate.

Two things have to be true before this exits zero:

  1. Each engine's verdict clears the SLO.
  2. The two engines AGREE -- same verdict, and numbers inside the divergence
     band from slo.json.

The second check is the one that earns its keep. A green JMeter run and a green
k6 run tell you nothing on their own; they can both be green because the load
generator never actually applied load. But if the two engines were pointed at the
same service under the same workload and they land 3x apart, one of them is
lying, and you do not get to pick which. That is a broken test, and a broken test
that reports PASS is worse than a red build -- so it fails the build too.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slo as slo_mod  # noqa: E402

BAR = "-" * 74


def divergence_pct(a, b):
    """Symmetric relative gap, as a percentage of the larger value."""
    hi = max(abs(a), abs(b))
    if hi == 0:
        return 0.0
    return abs(a - b) / hi * 100.0


def _row(label, jm, k6, unit=""):
    print(f"  {label:<14} jmeter={jm:>10}{unit:<4} k6={k6:>10}{unit:<4} "
          f"gap={divergence_pct(float(jm), float(k6)):>6.1f}%")


def main(argv):
    # `--expect-fail` inverts the SLO half of the gate: used by `make prove-teeth`,
    # where a fault is injected and a PASS is the bug.
    expect_fail = "--expect-fail" in argv
    paths = [a for a in argv[1:] if not a.startswith("--")]
    if len(paths) != 2:
        print("usage: cross_verify.py <jmeter.json> <k6.json> [--expect-fail]",
              file=sys.stderr)
        return 2

    cfg = slo_mod.load()
    band = cfg["cross_engine"]
    results = {}
    for p in paths:
        with open(p) as fh:
            r = json.load(fh)
        results[r["engine"]] = r

    if set(results) != {"jmeter", "k6"}:
        print(f"expected one jmeter and one k6 summary, got {sorted(results)}",
              file=sys.stderr)
        return 2

    jm, k6 = results["jmeter"], results["k6"]
    problems = []

    print(BAR)
    print(f"  CROSS-ENGINE VERIFY   service={cfg['service']}   "
          f"{'MODE: fault injected, both engines must FAIL' if expect_fail else ''}")
    print(BAR)

    # ---- 1. did the run even happen -------------------------------------
    for name, r in (("jmeter", jm), ("k6", k6)):
        if r["samples"] == 0:
            problems.append(f"{name} recorded zero steady-state samples")

    if problems:
        for p in problems:
            print(f"  !! {p}")
        print(BAR)
        return 1

    _row("samples", jm["samples"], k6["samples"])
    _row("p95", jm["metrics"]["p95_ms"], k6["metrics"]["p95_ms"], "ms")
    _row("p99", jm["metrics"]["p99_ms"], k6["metrics"]["p99_ms"], "ms")
    _row("throughput", jm["metrics"]["rps"], k6["metrics"]["rps"], "/s")
    _row("error rate", jm["metrics"]["error_rate"], k6["metrics"]["error_rate"])
    print()

    # ---- 2. agreement ----------------------------------------------------
    # Checked FIRST and always, including under --expect-fail. If the engines
    # disagree, neither number is worth reading.
    if jm["verdict"] != k6["verdict"]:
        problems.append(
            f"engines disagree on the verdict: jmeter={jm['verdict']} k6={k6['verdict']}. "
            f"One of these two scripts is not testing what the other is.")

    p95_gap = divergence_pct(jm["metrics"]["p95_ms"], k6["metrics"]["p95_ms"])
    if p95_gap > band["p95_divergence_pct"]:
        problems.append(
            f"p95 divergence {p95_gap:.1f}% exceeds the {band['p95_divergence_pct']}% band "
            f"({jm['metrics']['p95_ms']}ms vs {k6['metrics']['p95_ms']}ms) -- "
            f"the two engines are not applying the same workload")

    rps_gap = divergence_pct(jm["metrics"]["rps"], k6["metrics"]["rps"])
    if rps_gap > band["rps_divergence_pct"]:
        problems.append(
            f"throughput divergence {rps_gap:.1f}% exceeds the {band['rps_divergence_pct']}% band "
            f"({jm['metrics']['rps']}/s vs {k6['metrics']['rps']}/s)")

    # ---- 3. the SLO itself ----------------------------------------------
    for name, r in (("jmeter", jm), ("k6", k6)):
        print(f"  {name} verdict: {r['verdict']}")
        for b in r["breaches"]:
            print(f"      - {b}")

        if expect_fail and r["verdict"] == "PASS":
            problems.append(
                f"{name} reported PASS while {cfg['teeth']['latency_ms']}ms of latency was "
                f"injected into every request. The gate is not measuring anything.")
        elif not expect_fail and r["verdict"] == "FAIL":
            problems.append(f"{name} breached the SLO")

    print()
    if problems:
        print("  RESULT: FAIL")
        for p in problems:
            print(f"    x {p}")
        print(BAR)
        return 1

    if expect_fail:
        print("  RESULT: PASS -- both engines caught the injected fault. "
              "The gate has teeth.")
    else:
        print(f"  RESULT: PASS -- both engines agree, both inside SLO "
              f"(p95 gap {p95_gap:.1f}%, rps gap {rps_gap:.1f}%).")
    print(BAR)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
