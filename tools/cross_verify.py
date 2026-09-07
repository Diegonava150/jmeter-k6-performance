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
    # `--expect-divergence` inverts the AGREEMENT half: used by
    # `make prove-divergence`, where the two engines are deliberately given
    # different workloads and a clean run is the bug.
    expect_div = "--expect-divergence" in argv
    paths = [a for a in argv[1:] if not a.startswith("--")]
    if len(paths) != 2:
        print("usage: cross_verify.py <jmeter.json> <k6.json> "
              "[--expect-fail|--expect-divergence]", file=sys.stderr)
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
    divergence_problems = []

    profiles = {jm.get("profile"), k6.get("profile")}
    profile_name = jm.get("profile", "?")
    mode = ""
    if expect_fail:
        mode = "MODE: fault injected, both engines must FAIL"
    elif expect_div:
        mode = "MODE: engines desynchronised, the gate must REJECT this run"

    print(BAR)
    print(f"  CROSS-ENGINE VERIFY   service={cfg['service']}   "
          f"profile={profile_name}   {mode}")
    print(BAR)

    # Comparing a closed-model run against an open-model one is meaningless, and
    # it is an easy mistake to make from the shell. Catch it before anything else.
    if len(profiles) != 1:
        print(f"  !! summaries are from different profiles: {sorted(str(p) for p in profiles)}")
        print(BAR)
        return 2

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
    _row("p50", jm["metrics"]["p50_ms"], k6["metrics"]["p50_ms"], "ms")
    _row("p95", jm["metrics"]["p95_ms"], k6["metrics"]["p95_ms"], "ms")
    _row("p99", jm["metrics"]["p99_ms"], k6["metrics"]["p99_ms"], "ms")
    _row("rps  [gated]", jm["metrics"]["rps"], k6["metrics"]["rps"], "/s")
    _row("error rate", jm["metrics"]["error_rate"], k6["metrics"]["error_rate"])
    print("  (latency rows are informational and NOT gated: measured across four")
    print("   runs they do not separate an honest run from a deliberate desync.")
    print("   See cross_engine._readme in slo.json.)")
    print()

    # ---- 2. agreement ----------------------------------------------------
    # Checked FIRST and always, including under --expect-fail. If the engines
    # disagree, neither number is worth reading.
    if jm["verdict"] != k6["verdict"]:
        divergence_problems.append(
            f"engines disagree on the verdict: jmeter={jm['verdict']} k6={k6['verdict']}. "
            f"One of these two scripts is not testing what the other is.")

    # Signal 1: throughput. How much work was actually offered.
    rps_gap = divergence_pct(jm["metrics"]["rps"], k6["metrics"]["rps"])
    if rps_gap > band["rps_divergence_pct"]:
        divergence_problems.append(
            f"throughput divergence {rps_gap:.1f}% exceeds the {band['rps_divergence_pct']}% band "
            f"({jm['metrics']['rps']}/s vs {k6['metrics']['rps']}/s) -- "
            f"the two engines are not applying the same workload")

    # Signal 2: scenario mix. WHICH work was offered. Needed because throughput
    # is blind in the open profile, where both engines are pinned to the same
    # arrival rate by construction and only the endpoint mix could drift.
    mix_band = band["scenario_mix_divergence_pp"]
    labels = sorted(set(jm["per_scenario"]) | set(k6["per_scenario"]))
    mix_rows, worst_pp = [], 0.0
    for label in labels:
        a = jm["per_scenario"].get(label, {}).get("share_pct", 0.0)
        b = k6["per_scenario"].get(label, {}).get("share_pct", 0.0)
        pp = abs(a - b)
        worst_pp = max(worst_pp, pp)
        mix_rows.append((label, a, b, pp))
        if pp > mix_band:
            divergence_problems.append(
                f"scenario mix divergence on '{label}': {a:.1f}% of jmeter's traffic "
                f"vs {b:.1f}% of k6's, {pp:.1f}pp apart (band {mix_band}pp) -- "
                f"the engines are weighting the workload differently")

    print("  scenario mix  [gated]")
    for label, a, b, pp in mix_rows:
        flag = "  <-- over band" if pp > mix_band else ""
        print(f"    {label:<20} jmeter={a:>6.1f}%  k6={b:>6.1f}%  diff={pp:>5.1f}pp{flag}")
    print()

    # ---- 3. the SLO itself ----------------------------------------------
    for name, r in (("jmeter", jm), ("k6", k6)):
        print(f"  {name} verdict: {r['verdict']}")
        for b in r["breaches"]:
            print(f"      - {b}")

        if expect_fail and r["verdict"] == "PASS":
            problems.append(
                f"{name} reported PASS while {cfg['teeth']['latency_ms']}ms of latency was "
                f"injected into every request. The gate is not measuring anything.")
        elif not expect_fail and not expect_div and r["verdict"] == "FAIL":
            problems.append(f"{name} breached the SLO")

    print()

    # ---- 4. verdict ------------------------------------------------------
    if expect_div:
        # The engines were given provably different workloads. The gate is
        # required to have noticed, on the divergence bands alone -- and note
        # both engines may well still be inside the SLO, which is exactly the
        # case a build that only checks thresholds would wave through.
        if not divergence_problems:
            print("  RESULT: FAIL")
            print(f"    x the two engines ran deliberately different workloads "
                  f"(k6 at {cfg['divergence_probe']['load_multiplier']}x) and the gate "
                  f"did not notice.")
            print(f"    x rps gap {rps_gap:.1f}% (band {band['rps_divergence_pct']}%), "
                  f"worst scenario-mix gap {worst_pp:.1f}pp (band {mix_band}pp)")
            print("    x the divergence bands are too loose to detect a real mismatch.")
            print(BAR)
            return 1
        print("  RESULT: PASS -- the gate rejected a desynchronised run:")
        for d in divergence_problems:
            print(f"    - {d}")
        print(BAR)
        return 0

    problems = divergence_problems + problems
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
              f"(rps gap {rps_gap:.1f}%, worst mix gap {worst_pp:.1f}pp).")
    print(BAR)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
