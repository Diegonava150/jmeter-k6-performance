"""One summarizer, two engines.

This is the load-bearing idea of the repo. JMeter and k6 disagree about almost
everything: what a percentile is, whether to interpolate it, when a run starts,
what counts as an error, whether the ramp is part of the measurement. Compare
their built-in summaries and you are comparing two vendors' opinions, not two
measurements of a service.

So neither engine gets to report its own score. Each one emits raw per-request
records -- JMeter a .jtl, k6 a newline-delimited JSON stream -- and BOTH are
reduced to a verdict by `summarize()` below. Same percentile algorithm, same
ramp trim, same error rule, same clock arithmetic. Whatever bias that function
has, it has equally against both, which is the only way the comparison in
cross_verify.py means anything.

Run `python3 tools/summarize.py --self-test` to see that claim enforced.
"""
import csv
import json
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    ts_ms: int      # wall clock at which the request COMPLETED
    elapsed_ms: float
    label: str
    ok: bool


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------

def percentile(sorted_vals, p):
    """Nearest-rank percentile. No interpolation.

    Deliberately the simplest defensible definition. JMeter interpolates, k6
    does something else again, and the difference between the two is real but
    tiny -- which is exactly the problem: it is a difference big enough to move
    a borderline verdict and small enough that nobody notices it did. Both
    engines get this function instead.
    """
    if not sorted_vals:
        return 0.0
    if p <= 0:
        return float(sorted_vals[0])
    rank = -(-len(sorted_vals) * p // 100)          # ceil, integer arithmetic
    return float(sorted_vals[min(rank, len(sorted_vals)) - 1])


# --------------------------------------------------------------------------
# Parsers -- the ONLY engine-specific code in this file
# --------------------------------------------------------------------------

def parse_jtl(path):
    """JMeter CSV results file. Fields located by header name, not position."""
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if not row.get("timeStamp"):
                continue
            try:
                out.append(Sample(
                    ts_ms=int(row["timeStamp"]),
                    elapsed_ms=float(row["elapsed"]),
                    label=row.get("label", "?"),
                    ok=row.get("success", "").strip().lower() == "true",
                ))
            except (ValueError, KeyError):
                continue
    return out


_FRAC = re.compile(r"\.(\d+)")


def _rfc3339_ms(text):
    """k6 timestamps carry nanoseconds; datetime tops out at microseconds."""
    text = text.strip().replace("Z", "+00:00")
    text = _FRAC.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text)
    from datetime import datetime
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def parse_k6_json(path):
    """k6 --out json=... stream. We read http_req_duration points only."""
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or '"http_req_duration"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "Point" or rec.get("metric") != "http_req_duration":
                continue
            d = rec.get("data", {})
            tags = d.get("tags", {}) or {}
            # k6 sets expected_response=false for a non-2xx/3xx, which is the
            # closest analogue to JMeter's success flag.
            expected = tags.get("expected_response")
            if expected is not None:
                ok = str(expected).lower() == "true"
            else:
                ok = str(tags.get("status", "0")).startswith(("2", "3"))
            out.append(Sample(
                ts_ms=_rfc3339_ms(d["time"]),
                elapsed_ms=float(d["value"]),
                label=tags.get("scenario") or tags.get("name") or "?",
                ok=ok,
            ))
    return out


# --------------------------------------------------------------------------
# The shared reduction
# --------------------------------------------------------------------------

def summarize(samples, prof, engine, profile_name="closed"):
    """Raw samples -> verdict. Identical arithmetic for every engine.

    `prof` is one entry from slo.json's `profiles`, already resolved by the
    caller, so this function never has to know which profile is "current".
    """
    ramp_ms = prof["ramp_up_s"] * 1000
    thresholds = prof["slo"]

    if not samples:
        return {
            "engine": engine, "profile": profile_name, "verdict": "FAIL", "samples": 0,
            "breaches": ["no samples recorded -- the run produced nothing to judge"],
        }

    # ONE trim rule for both engines: discard everything in the ramp window,
    # measured from the first request either engine sent. An engine that scored
    # its own ramp would look slower for a reason that has nothing to do with
    # the service.
    t0 = min(s.ts_ms for s in samples)
    steady = [s for s in samples if s.ts_ms >= t0 + ramp_ms]
    if not steady:
        return {
            "engine": engine, "profile": profile_name, "verdict": "FAIL",
            "samples": len(samples),
            "breaches": [f"every sample fell inside the {ramp_ms}ms ramp window"],
        }

    lat = sorted(s.elapsed_ms for s in steady)
    failures = sum(1 for s in steady if not s.ok)
    span_s = (max(s.ts_ms for s in steady) - min(s.ts_ms for s in steady)) / 1000.0
    # A single-sample window has zero span; treat throughput as undefined-but-low
    # rather than dividing by zero.
    rps = (len(steady) / span_s) if span_s > 0 else 0.0

    m = {
        # p50 is here for the cross-engine comparison, not for the SLO. It is the
        # statistic that survives a transient stall on one load generator, which
        # the tail does not.
        "p50_ms": round(percentile(lat, 50), 2),
        "p95_ms": round(percentile(lat, 95), 2),
        "p99_ms": round(percentile(lat, 99), 2),
        "error_rate": round(failures / len(steady), 5),
        "rps": round(rps, 2),
    }

    breaches = []
    if m["p95_ms"] > thresholds["p95_ms"]:
        breaches.append(f"p95 {m['p95_ms']}ms > {thresholds['p95_ms']}ms")
    if m["p99_ms"] > thresholds["p99_ms"]:
        breaches.append(f"p99 {m['p99_ms']}ms > {thresholds['p99_ms']}ms")
    if m["error_rate"] > thresholds["error_rate"]:
        breaches.append(f"error rate {m['error_rate']:.3%} > {thresholds['error_rate']:.3%}")
    # Optional on purpose. A closed model's throughput is vus/(think+latency), a
    # property of the generator, so a threshold on it there cannot fire before the
    # latency gate does. See tools/prove_reachable.py.
    if "min_rps" in thresholds and m["rps"] < thresholds["min_rps"]:
        breaches.append(f"throughput {m['rps']}/s < {thresholds['min_rps']}/s")

    by_label = {}
    for s in steady:
        by_label.setdefault(s.label, []).append(s.elapsed_ms)

    return {
        "engine": engine,
        "profile": profile_name,
        "verdict": "FAIL" if breaches else "PASS",
        "samples": len(steady),
        "discarded_ramp_samples": len(samples) - len(steady),
        "window_s": round(span_s, 2),
        "metrics": m,
        # share_pct is the timing-independent fingerprint of the workload: what
        # FRACTION of requests went to each endpoint. Two engines running the same
        # plan must agree on this whatever the host is doing to their clocks.
        "per_scenario": {
            k: {
                "samples": len(v),
                "share_pct": round(len(v) / len(steady) * 100, 2),
                "p95_ms": round(percentile(sorted(v), 95), 2),
            }
            for k, v in sorted(by_label.items())
        },
        "breaches": breaches,
    }


PARSERS = {"jmeter": parse_jtl, "k6": parse_k6_json}


def main(argv):
    sys.path.insert(0, __import__("os").path.dirname(__file__))
    import slo as slo_mod

    if "--self-test" in argv:
        cfg = slo_mod.load()
        name, prof = slo_mod.profile(cfg, "closed")
        ramp = prof["ramp_up_s"] * 1000
        base = 1_700_000_000_000
        # A synthetic run that actually spans the ramp: the first `ramp`
        # milliseconds are deliberately awful and must be trimmed away by both
        # engines alike, and the steady window that follows must be scored.
        raw = [(base + i * 10, 5000.0, "browse_products", False)
               for i in range(ramp // 10)]
        raw += [(base + ramp + i * 10, 100.0 + (i % 200), "browse_products", True)
                for i in range(400)]
        a = summarize([Sample(*r) for r in raw], prof, "jmeter", name)
        b = summarize([Sample(*r) for r in raw], prof, "k6", name)
        del a["engine"], b["engine"]
        assert a == b, "summarize() is not engine-blind"
        assert a["discarded_ramp_samples"] == ramp // 10, "ramp was not trimmed"
        assert a["verdict"] == "PASS", f"expected the steady window to pass, got {a}"

        # Every profile must reduce without blowing up, including the open one
        # whose SLO carries a key the closed one does not.
        for pname in cfg["profiles"]:
            _, pr = slo_mod.profile(cfg, pname)
            r = summarize([Sample(*x) for x in raw], pr, "k6", pname)
            assert r["profile"] == pname and "metrics" in r, pname

        print("self-test OK")
        print("  identical samples   -> identical verdict for both engines")
        print(f"  ramp trimmed        -> {a['discarded_ramp_samples']} samples discarded, "
              f"{a['samples']} scored")
        print(f"  profiles reduced    -> {', '.join(cfg['profiles'])}")
        print(f"  metrics             -> {json.dumps(a['metrics'])}")
        return 0

    if len(argv) < 4:
        print("usage: summarize.py <jmeter|k6> <raw-file> <out.json> [profile]",
              file=sys.stderr)
        return 2
    engine, raw_path, out_path = argv[1], argv[2], argv[3]
    cfg = slo_mod.load()
    name, prof = slo_mod.profile(cfg, argv[4] if len(argv) > 4 else None)

    samples = PARSERS[engine](raw_path)
    result = summarize(samples, prof, engine, name)
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2)

    print(f"[{engine}/{name}] {result['verdict']}  "
          f"samples={result['samples']}  "
          f"p95={result.get('metrics', {}).get('p95_ms', '-')}ms  "
          f"rps={result.get('metrics', {}).get('rps', '-')}")
    for b in result["breaches"]:
        print(f"         breach: {b}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
