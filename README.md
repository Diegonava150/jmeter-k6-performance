<h1 align="center">jmeter-k6-performance</h1>

<p align="center">
  <b>One workload. Two engines. One gate that fails when they disagree.</b>
</p>

<p align="center">
  <a href="https://github.com/Diegonava150/jmeter-k6-performance/actions/workflows/ci.yml">
    <img src="https://github.com/Diegonava150/jmeter-k6-performance/actions/workflows/ci.yml/badge.svg" alt="cross-engine load" />
  </a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Apache%20JMeter-5.6.3-D22128?style=for-the-badge&logo=apachejmeter&logoColor=white" alt="JMeter" />
  <img src="https://img.shields.io/badge/Grafana%20k6-v2-7D64FF?style=for-the-badge&logo=k6&logoColor=white" alt="k6" />
  <img src="https://img.shields.io/badge/Toxiproxy-fault%20injection-14B8A6?style=for-the-badge&logoColor=white" alt="Toxiproxy" />
  <img src="https://img.shields.io/badge/PostgREST%20%2B%20Postgres-hermetic-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="Stack" />
  <img src="https://img.shields.io/badge/Docker-only%20prerequisite-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker" />
</p>

---

## The problem this repo is about

A load test that reports **PASS** is not evidence. It is a claim, and the claim has
two independent ways of being wrong that a green build hides equally well:

1. **The test did not apply the load it says it did.** A misconfigured ramp, a
   think time in the wrong unit, a connection pool the generator never saturated.
   The service was never stressed, so of course it held.
2. **The threshold cannot fail.** A p95 gate set at 5000ms on a service that
   answers in 40ms is green for exactly as long as it is useless, and nobody
   finds out until the incident.

Neither shows up in a report. Both show up as a passing build.

This repo attacks both. The same workload is executed by **JMeter** and by **k6**,
and the build fails if the two engines disagree — because a result that depends on
which tool produced it is measuring the tool, not the service. Then a real fault is
injected and both engines are *required* to fail, because a gate that has never
been seen to fail has not been shown to work.

---

## How it fits together

```
                         slo.json
              (workload + thresholds + bands)
                             │
        ┌────────────────────┴────────────────────┐
        │                                         │
   gen_jmeter_props.py                      open() at init
        │                                         │
   ┌────▼─────┐                             ┌─────▼────┐
   │  JMeter  │                             │    k6    │
   │ .jmx     │                             │  .js     │
   └────┬─────┘                             └─────┬────┘
        │  raw .jtl                    raw NDJSON │
        └────────────────┬────────────────────────┘
                         │
              tools/summarize.py          ← ONE reduction, both engines
             (same percentile, same
              ramp trim, same error rule)
                         │
              ┌──────────┴──────────┐
        jmeter.summary.json    k6.summary.json
              └──────────┬──────────┘
                         │
             tools/cross_verify.py         ← THE GATE
             ├─ each engine inside SLO?
             ├─ do the verdicts agree?
             └─ are p95 / rps inside the divergence band?
```

And the system under test, entirely local:

```
  jmeter / k6  ──▶  toxiproxy :8080  ──▶  postgrest :3000  ──▶  postgres :5432
                    (injects latency          8-conn pool         50k products
                     on demand)                                   20k orders
```

---

## Quick start

Prerequisites: **Docker**, **Java 21**, **JMeter 5.6+**, **k6**, **Python 3.11+**.

```bash
make test          # no Docker needed: summarizer + gate unit tests
make up            # start Postgres + PostgREST + Toxiproxy, wait for readiness
make load          # run BOTH engines, then cross-verify
make prove-teeth   # inject latency, assert BOTH engines catch it
make down
```

---

## The three ideas

### 1. `slo.json` is the only place a number lives

Not "the config is shared" in the loose sense — there is genuinely no VU count,
duration, ramp, think time, path or threshold written in `workload.jmx` or
`workload.js`. k6 reads `slo.json` with `open()` at init. JMeter cannot read JSON,
so `tools/gen_jmeter_props.py` projects the same file into a `.properties` file
that every `${__P(...)}` in the plan resolves against.

Change `vus` from 20 to 50 in one place and both engines change together. Change
it in only one of them and the divergence check below catches you.

### 2. Neither engine is allowed to score itself

This is the part that makes the comparison mean anything.

JMeter and k6 disagree about nearly everything that matters to a verdict: whether
percentiles interpolate, when the run clock starts, what counts as an error,
whether the ramp is part of the measurement. Compare their built-in summaries and
you are comparing two vendors' opinions.

So neither summary is used. Each engine emits **raw per-request records** — JMeter a
`.jtl`, k6 a newline-delimited JSON stream — and both are reduced to a verdict by a
single function in [`tools/summarize.py`](tools/summarize.py): same nearest-rank
percentile, same ramp trim measured from the first request, same error rule, same
throughput arithmetic. Whatever bias that function has, it has *equally against
both*.

That property is not assumed. `make self-test` feeds one synthetic run through the
reduction twice, labelled as each engine, and asserts the verdicts are byte-identical:

```
$ make self-test
self-test OK
  identical samples   -> identical verdict for both engines
  ramp trimmed        -> 1000 samples discarded, 400 scored
  metrics             -> {"p95_ms": 289.0, "p99_ms": 297.0, "error_rate": 0.0, "rps": 100.25}
```

k6's native `thresholds` are still declared, scoped to `{phase:steady}` so they
ignore the ramp too. They are not what the build gates on — they are there because
they are the idiomatic way to read a k6 run, and because when they disagree with
the shared summarizer, *that disagreement is itself information*.

### 3. Disagreement is a failure, not a footnote

`tools/cross_verify.py` checks agreement **before** it checks the SLO, and it checks
agreement even in fault-injection mode:

- **Verdict agreement.** JMeter says PASS and k6 says FAIL → build fails. You do
  not get to pick the answer you like.
- **Divergence bands.** If the two engines were pointed at the same service under
  the same workload and their p95s land more than `p95_divergence_pct` apart, one
  of them is lying and the run is void — *regardless of which way it fell*. A green
  build built on two tests that are not the same test is the thing this repo exists
  to prevent.

A wide gap is not a slow service. It is a broken test. Broken tests fail the build.

---

## The gate is itself tested

An untested gate is the thing this repo exists to complain about, so the gate does
not get to be the exception.

`make prove-teeth` proves the *thresholds* bite, using a real fault against the real
stack. But the failure modes that matter most in `cross_verify.py` are ones a
healthy service will never produce on its own — two engines returning opposite
verdicts, two engines returning numbers that cannot both be true, a run that
recorded nothing at all being scored as a pass. Those are driven from crafted
summaries in [`tools/test_gate.py`](tools/test_gate.py), asserting exit codes,
because the exit code is what CI actually reads:

```
$ make test
>> summarizer self-test
self-test OK
  identical samples   -> identical verdict for both engines
  ramp trimmed        -> 1000 samples discarded, 400 scored
>> gate tests
  PASS  agreeing engines, both inside SLO                  (exit 0, expected 0)
  PASS  fault injected, both engines FAIL                  (exit 0, expected 0)
  PASS  engines return opposite verdicts                   (exit 1, expected 1)
  PASS  p95 diverges beyond the band though both say PASS  (exit 1, expected 1)
  PASS  throughput diverges beyond the band                (exit 1, expected 1)
  PASS  a genuine SLO breach both engines agree on         (exit 1, expected 1)
  PASS  engine reports PASS while a fault is injected      (exit 1, expected 1)
  PASS  a run that recorded nothing is not a pass          (exit 1, expected 1)

all 8 gate tests passed: the gate rejects what it is supposed to reject
```

Note the fourth case: both engines say PASS, both are inside the SLO, and the gate
still fails the build — because a 3x gap on p95 means the two scripts were never
running the same test, and a green build assembled from two incomparable runs is
worth less than a red one. This needs no Docker and runs first in CI.

---

## What a clean run looks like

Measured on this repo's own stack (20 VUs, 60s steady window, 400k products):

```
$ make load
[jmeter] PASS  samples=3801  p95=37.0ms   rps=63.9
[k6]     PASS  samples=4067  p95=30.68ms  rps=62.6
--------------------------------------------------------------------------
  CROSS-ENGINE VERIFY   service=catalog-api
--------------------------------------------------------------------------
  samples        jmeter=      3801     k6=      4067     gap=   6.5%
  p95            jmeter=      37.0ms   k6=     30.68ms   gap=  17.1%
  p99            jmeter=      45.0ms   k6=     38.38ms   gap=  14.7%
  throughput     jmeter=      63.9/s   k6=      62.6/s   gap=   2.0%
  error rate     jmeter=       0.0     k6=       0.0     gap=   0.0%

  jmeter verdict: PASS
  k6 verdict: PASS

  RESULT: PASS -- both engines agree, both inside SLO (p95 gap 17.1%, rps gap 2.0%).
--------------------------------------------------------------------------
```

Two independent load generators, built from one config, landing 2.0% apart on
throughput. That number is the point of the repo: it is the evidence that the
workload each engine applied was actually the same workload, which is what makes
the shared PASS worth anything.

The same run on a GitHub-hosted runner, where nothing else is competing for the
CPU, tightens to a **3.3% p95 gap and a 3.4% throughput gap** — and `prove-teeth`
on that same runner put the two engines 0.3% apart at 743.0 vs 741.0 ms. The
agreement is a property of the setup, not of one lucky laptop.

The per-scenario breakdown in `reports/*.summary.json` shows the fast and slow paths
separating exactly as the schema intends — JMeter's view of the same run:

| scenario | share of traffic | p95 |
|---|---|---|
| `browse_products` | 70.0% (2661) | 12.0 ms |
| `search_products` | 20.0% (760) | 43.0 ms |
| `read_orders` | 10.0% (380) | 13.0 ms |

The traffic mix lands on 70.0 / 20.0 / 10.0 against the weights declared in
`slo.json`, which is itself a check: JMeter's throughput controllers and k6's
cumulative-weight roll are two completely different mechanisms that have to produce
the same distribution, or the workloads were never comparable.

---

## `make prove-teeth` — mutation testing, for a load test

Every threshold in this repo was typed by a human who believed it was tight. The
only way to know it still bites is to make it bite on purpose.

`make prove-teeth` injects `teeth.latency_ms` of real latency at the network hop
between the load generators and the API using Toxiproxy, runs both engines, and then
**inverts the gate**: a `PASS` is now the bug.

```
$ make prove-teeth
>> injecting fault
injected 600ms +/-150ms latency into every response
...
--------------------------------------------------------------------------
  CROSS-ENGINE VERIFY   service=catalog-api   MODE: fault injected, both engines must FAIL
--------------------------------------------------------------------------
  samples        jmeter=      1307     k6=      1391     gap=   6.0%
  p95            jmeter=     744.0ms   k6=    743.24ms   gap=   0.1%
  p99            jmeter=     774.0ms   k6=    763.37ms   gap=   1.4%
  throughput     jmeter=     21.97/s   k6=     21.41/s   gap=   2.5%

  jmeter verdict: FAIL
      - p95 744.0ms > 400ms
  k6 verdict: FAIL
      - p95 743.24ms > 400ms

  RESULT: PASS -- both engines caught the injected fault. The gate has teeth.
--------------------------------------------------------------------------
fault removed
```

If either engine had reported PASS with 600ms bolted onto every single response,
that engine is not measuring the service and the build fails — loudly, because that
is the failure mode nobody ever notices on their own.

Two details in that output are worth more than the verdict:

- **The engines converged to 0.1% on p95** (744.0 vs 743.24ms). When the signal is
  dominated by a fault of known size, the two independent generators agree almost
  exactly. That is the cleanest available evidence that the 17% gap on a clean run
  is engine noise around a small number rather than two different workloads.
- **Throughput fell from ~63/s to ~21/s** without a single error. This is what a
  saturated service looks like from the outside: every response still `200 OK`,
  nothing in the error rate, and a third of the work getting done. A gate watching
  only status codes sees a perfectly healthy service here.

This runs in CI on every push as **its own required status check**, not as a step
inside the load job — and `main` is protected such that it must pass before
anything merges.

That separation is deliberate. A green *Cross-engine load* claims the service met
its SLO today. A green *Prove the gate has teeth* claims the SLO check is still
capable of failing at all. Those are different claims, and only the second one
rots silently, so it does not get to hide as step seven of something else.

The threshold is re-proven on every commit, not trusted.

---

## The system under test is part of the repo

Everything runs against a stack this repository starts: Postgres seeded with 50k
products and 20k orders, a real PostgREST API over it, and Toxiproxy in the path.

That is a deliberate choice over a public demo endpoint. A shared endpoint's numbers
move with someone else's traffic and its rate limits look exactly like the service
degrading, so nothing you measure is attributable and nobody can reproduce it. Here,
`git clone && make load` produces the same numbers on any machine, offline, and the
only prerequisite is Docker.

The workload has a fast path and a slow path on purpose. `products.name` is
deliberately left un-indexed for `LIKE`, so `search_products` does a genuine
sequential scan while `browse_products` hits an index. The API pool is capped at 8
connections against a 50-connection database, so saturation surfaces as latency
rather than as connection errors. A load test where every request costs the same
measures nothing interesting.

---

## Layout

```
slo.json                    single source of truth: workload, thresholds, bands
docker-compose.yml          postgres + postgrest + toxiproxy
Makefile                    up / load / prove-teeth / report / down

jmeter/workload.jmx         plan shape only; every value is ${__P(...)}
jmeter/user.properties      lean .jtl output (no response bodies during measurement)
k6/workload.js              reads slo.json via open(); no literals

tools/slo.py                config loader + weight bands
tools/gen_jmeter_props.py   slo.json  ->  JMeter .properties
tools/summarize.py          THE shared reduction, + --self-test
tools/cross_verify.py       THE gate: SLO + agreement + divergence
tools/test_gate.py          tests that the gate REJECTS what it claims to reject
tools/toxic.py              Toxiproxy fault injection (stdlib only)
tools/wait_for_stack.py     readiness by real response, not by open port

stack/init.sql              schema + 70k seeded rows + PostgREST roles
stack/toxiproxy.json        proxy definition
```

---

## Notes on choices a reviewer might question

**Why run the engines sequentially rather than in parallel?** Two load generators on
one box contend for the same CPU and NIC. Run them together and the divergence check
starts measuring the laptop instead of the service — which would turn the repo's
central check into noise.

**Why no listener element in the `.jmx`?** An embedded listener during a CLI load run
is a well-known way to measure your own disk. JMeter writes the `.jtl` via `-l`;
`make report` generates the HTML dashboard afterwards, from the same file.

**Why nearest-rank percentiles instead of interpolated?** Because it is the simplest
definition both engines can be held to. The difference from interpolation is small —
which is exactly the danger: big enough to move a borderline verdict, small enough
that nobody notices it did.

**Why is the ramp discarded?** Both engines trim the same window, measured from the
first request. Scoring the ramp punishes an engine for how it warms up rather than
for what the service did.

**Why is p99 not in the divergence band?** Because it is not a stable enough signal
to gate on, and three runs of this repo show it:

| run | p99 (jmeter / k6) | **p99 gap** | p95 gap | rps gap |
|---|---|---|---|---|
| 50k-row dataset, ~15ms tail | 30.0 / 18.16 ms | **39.5%** | 6.0% | 2.7% |
| 400k-row dataset | 45.0 / 38.38 ms | **14.7%** | 17.1% | 2.0% |
| 400k-row dataset, *identical config* | 86.0 / 84.33 ms | **1.9%** | 17.7% | 2.0% |

JMeter records elapsed time as whole milliseconds; k6 keeps sub-millisecond floats.
That quantisation is a fixed absolute error, so how large it *looks* depends
entirely on how far out the tail happens to land — and the last two rows are the
same code, same dataset, same `slo.json`, on consecutive runs. The engines did not
become more alike between them; the denominator moved.

A signal that swings from 39.5% to 1.9% while the thing it is supposed to detect
never changes is not measuring whether the two workloads match, which is the only
job the divergence check has. p95 and throughput stay put across all three runs, so
those gate the build. p99 is still evaluated against the SLO by each engine
independently and still printed side by side — it just is not treated as evidence
that the workloads differ. A band you have to keep widening to stay green is not a
band.

---

<p align="center"><sub>
Diego Navarro Ruiz · QA Automation Engineer ·
<a href="https://github.com/Diegonava150">github.com/Diegonava150</a>
</sub></p>
