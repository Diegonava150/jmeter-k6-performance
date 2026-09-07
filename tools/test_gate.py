"""Tests for the gate itself.

This repo's whole argument is that a check nobody has watched fail is not known
to work. `make prove-teeth` makes that argument about the SLO thresholds, using
a real fault against the real stack. This file makes the same argument about
cross_verify.py, using crafted summaries -- because the failure modes that matter
most here are the ones a healthy service will never produce on its own:

  - the two engines returning opposite verdicts
  - the two engines returning numbers that cannot both be true
  - a run that recorded nothing at all still being scored

Every case below asserts an exit code, because that is what CI actually reads.
Run with `make test`.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "cross_verify.py")


def summary(engine, verdict, p95, rps, samples=3000, breaches=None,
            profile="closed", p50=None, mix=None):
    return {
        "engine": engine,
        "profile": profile,
        "verdict": verdict,
        "samples": samples,
        "window_s": 60.0,
        "metrics": {"p50_ms": p95 / 4 if p50 is None else p50,
                    "p95_ms": p95, "p99_ms": p95 * 1.2,
                    "error_rate": 0.0, "rps": rps},
        "per_scenario": mix or {
            "browse_products": {"samples": 1, "share_pct": 70.0, "p95_ms": p95},
            "search_products": {"samples": 1, "share_pct": 20.0, "p95_ms": p95},
            "read_orders":     {"samples": 1, "share_pct": 10.0, "p95_ms": p95},
        },
        "breaches": breaches or [],
    }


def run_gate(jm, k6, *flags):
    with tempfile.TemporaryDirectory() as d:
        pj, pk = os.path.join(d, "jm.json"), os.path.join(d, "k6.json")
        for path, doc in ((pj, jm), (pk, k6)):
            with open(path, "w") as fh:
                json.dump(doc, fh)
        proc = subprocess.run(
            [sys.executable, GATE, pj, pk, *flags],
            capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr


CASES = []


def case(name, expect_rc, expect_text=None):
    def deco(fn):
        CASES.append((name, fn, expect_rc, expect_text))
        return fn
    return deco


# ---------------------------------------------------------------- passing ---

@case("agreeing engines, both inside SLO", 0)
def _():
    return run_gate(summary("jmeter", "PASS", 37.0, 63.9),
                    summary("k6", "PASS", 30.7, 62.6))


@case("fault injected, both engines FAIL", 0, "has teeth")
def _():
    return run_gate(
        summary("jmeter", "FAIL", 744.0, 21.9, breaches=["p95 744.0ms > 400ms"]),
        summary("k6", "FAIL", 743.2, 21.4, breaches=["p95 743.2ms > 400ms"]),
        "--expect-fail")


# ---------------------------------------------------------------- failing ---

@case("engines return opposite verdicts", 1, "disagree on the verdict")
def _():
    # Numbers deliberately close enough to clear the divergence bands, so the
    # ONLY thing that can catch this is the verdict-agreement check.
    return run_gate(summary("jmeter", "PASS", 390.0, 60.0),
                    summary("k6", "FAIL", 410.0, 60.0,
                            breaches=["p95 410.0ms > 400ms"]))


@case("scenario mix diverges though throughput matches", 1, "scenario mix divergence")
def _():
    # The open-profile blind spot: both engines pinned to the same arrival rate,
    # so throughput is identical by construction, but k6 is sending most of its
    # traffic to the cheap endpoint. Same rate, different work.
    heavy = {"browse_products": {"samples": 1, "share_pct": 70.0, "p95_ms": 4.0},
             "search_products": {"samples": 1, "share_pct": 20.0, "p95_ms": 21.0},
             "read_orders":     {"samples": 1, "share_pct": 10.0, "p95_ms": 4.0}}
    light = {"browse_products": {"samples": 1, "share_pct": 95.0, "p95_ms": 4.0},
             "search_products": {"samples": 1, "share_pct": 2.0, "p95_ms": 21.0},
             "read_orders":     {"samples": 1, "share_pct": 3.0, "p95_ms": 4.0}}
    return run_gate(summary("jmeter", "PASS", 18.0, 200.0, mix=heavy),
                    summary("k6", "PASS", 6.0, 200.0, mix=light))


@case("a tail-only difference does NOT void the run", 0)
def _():
    # The real case this repo measured: one engine took a transient stall, so the
    # tails are 35% apart while throughput and mix are identical. The workloads
    # matched; only the worst moments on one process differed. Gating on the tail
    # would have thrown away a valid run.
    return run_gate(summary("jmeter", "PASS", 17.0, 200.06, p50=2.0),
                    summary("k6", "PASS", 26.4, 200.01, p50=2.0))


@case("a large median gap on tiny latencies is quantisation, not divergence", 0)
def _():
    # 6.0ms vs 3.24ms is a 46% gap and was measured on an HONEST run: JMeter
    # records whole milliseconds, so at these values the percentage is noise.
    return run_gate(summary("jmeter", "PASS", 38.0, 63.34, p50=6.0),
                    summary("k6", "PASS", 32.18, 62.57, p50=3.24))


@case("throughput diverges beyond the band", 1, "throughput divergence")
def _():
    return run_gate(summary("jmeter", "PASS", 40.0, 63.0),
                    summary("k6", "PASS", 41.0, 20.0))


@case("a genuine SLO breach both engines agree on", 1, "breached the SLO")
def _():
    return run_gate(
        summary("jmeter", "FAIL", 744.0, 21.9, breaches=["p95 744.0ms > 400ms"]),
        summary("k6", "FAIL", 743.2, 21.4, breaches=["p95 743.2ms > 400ms"]))


@case("engine reports PASS while a fault is injected", 1, "not measuring anything")
def _():
    # The blind-gate case. This is the one that has to fail loudly, because it is
    # the one that otherwise ships a permanently green build.
    return run_gate(summary("jmeter", "PASS", 30.0, 63.0),
                    summary("k6", "PASS", 31.0, 62.0),
                    "--expect-fail")


@case("a run that recorded nothing is not a pass", 1, "zero steady-state samples")
def _():
    return run_gate(
        {"engine": "jmeter", "profile": "closed", "verdict": "FAIL",
         "samples": 0, "breaches": []},
        summary("k6", "PASS", 30.0, 62.0))


@case("a closed-model run compared against an open-model one", 2,
      "different profiles")
def _():
    # Easy to do from the shell, and meaningless: the two profiles apply
    # different arrival patterns, so any gap between them says nothing.
    return run_gate(summary("jmeter", "PASS", 37.0, 63.9, profile="closed"),
                    summary("k6", "PASS", 35.0, 195.0, profile="open"))


# ------------------------------------------------- prove-divergence mode ---

@case("desynchronised engines are caught by the bands", 0,
      "rejected a desynchronised run")
def _():
    # Both inside the SLO, both saying PASS, and the run is still void: k6 was
    # run at 2x, so throughput is nowhere near JMeter's. A threshold-only build
    # would wave this through.
    return run_gate(summary("jmeter", "PASS", 37.0, 63.9),
                    summary("k6", "PASS", 41.0, 127.0),
                    "--expect-divergence")


@case("bands too loose to catch a desync is itself a failure", 1,
      "did not notice")
def _():
    # The engines were deliberately desynchronised and the gate saw nothing.
    # That means the bands cannot detect a real mismatch, so the check that the
    # whole repo rests on is decorative -- which has to fail loudly.
    return run_gate(summary("jmeter", "PASS", 37.0, 63.9),
                    summary("k6", "PASS", 38.0, 64.5),
                    "--expect-divergence")


def main():
    width = max(len(n) for n, *_ in CASES)
    failures = 0
    for name, fn, expect_rc, expect_text in CASES:
        rc, out = fn()
        ok = rc == expect_rc and (expect_text is None or expect_text in out)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  "
              f"(exit {rc}, expected {expect_rc})")
        if not ok:
            failures += 1
            if expect_text and expect_text not in out:
                print(f"        expected to see {expect_text!r} in the output")
            print("        " + out.replace("\n", "\n        ").strip())
    print()
    if failures:
        print(f"{failures} of {len(CASES)} gate tests FAILED")
        return 1
    print(f"all {len(CASES)} gate tests passed: "
          f"the gate rejects what it is supposed to reject")
    return 0


if __name__ == "__main__":
    sys.exit(main())
