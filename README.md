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
make test              # no Docker: summarizer, gate, and threshold-reachability checks
make up                # start Postgres + PostgREST + Toxiproxy, wait for readiness
make load              # closed profile: both engines, then cross-verify
make load-open         # open profile: fixed arrival rate, service can saturate
make load-all          # both profiles
make prove-teeth       # inject latency, assert BOTH engines catch it
make prove-divergence  # desync the engines, assert the GATE catches that
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

`tools/cross_verify.py` checks agreement **before** it checks the SLO, and it
checks agreement even in fault-injection mode. It asks one narrow question — *did
the two engines apply the same workload?* — and it is deliberately **not** scored
on latency.

That last part was a correction, not a design choice. The check originally gated
on p95 divergence. Then four runs were measured — an honest closed run, an honest
open run, a faulted run, and a deliberate 2x desync:

| run | median gap | **throughput gap** |
|---|---|---|
| closed, honest | 46.0% | **1.2%** |
| open, honest | 6.0% | **0.0%** |
| faulted (600ms injected) | 1.5% | **2.9%** |
| **closed, deliberate 2x desync** | **63.8%** | **49.5%** |

Latency does not separate an honest run from a sabotaged one — an honest run
scored 46% and the sabotage scored 63.8%. At a 2–6ms median, JMeter's
whole-millisecond quantisation is already tens of percent, so the number is mostly
rounding. Throughput separates by more than an order of magnitude.

So the gate scores two things:

- **Throughput.** How much work was actually offered. 0.0–2.9% on every honest
  run, 49.5% on the desync.
- **Scenario mix.** *Which* work was offered — the share of traffic each endpoint
  received, compared in percentage points. This exists because throughput is
  blind in the open profile, where both engines are pinned to the same arrival
  rate by construction and the only thing that could drift is the endpoint mix.

Latency is still printed side by side, because a gap there deserves a human's
attention even when it does not deserve a red build.

The alternative was widening the latency band until it stopped complaining. This
README says elsewhere that a band you keep widening is not a band; the same logic
says a signal that cannot tell sabotage from noise should be removed, not tuned.

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

Two workload profiles ship. `make load` runs the closed model, `make load-open`
the open one, `make load-all` both.

```
$ make load-open
--------------------------------------------------------------------------
  CROSS-ENGINE VERIFY   service=catalog-api   profile=open
--------------------------------------------------------------------------
  samples        jmeter=     11971     k6=     12001     gap=   0.2%
  p50            jmeter=       2.0ms   k6=      1.88ms   gap=   6.0%
  p95            jmeter=      18.0ms   k6=     17.82ms   gap=   1.0%
  p99            jmeter=      21.0ms   k6=     21.96ms   gap=   4.4%
  rps  [gated]   jmeter=    200.08/s   k6=    200.04/s   gap=   0.0%
  error rate     jmeter=       0.0     k6=       0.0     gap=   0.0%

  scenario mix  [gated]
    browse_products      jmeter=  70.0%  k6=  70.5%  diff=  0.5pp
    read_orders          jmeter=  10.0%  k6=   9.7%  diff=  0.3pp
    search_products      jmeter=  20.0%  k6=  19.9%  diff=  0.1pp

  RESULT: PASS -- both engines agree, both inside SLO (rps gap 0.0%, worst mix gap 0.5pp).
--------------------------------------------------------------------------
```

Two independent load generators, built from one config, landing **0.0% apart on
throughput and 0.5pp apart on traffic mix**. Those numbers are the point of the
repo: they are the evidence that the workload each engine applied was actually the
same workload, which is what makes the shared PASS worth anything.

### Two profiles, because one of them cannot see saturation

| | `closed` | `open` |
|---|---|---|
| what is held fixed | 20 VUs + 300ms think time | 200 req/s arrival rate |
| when the service slows | **load backs off** | **a queue builds** |
| throughput is | a property of the generator | a property of the service |
| can show saturation | no | yes |

The closed profile is the familiar one and it has a structural blind spot: every
VU waits for its own response before sending again, so a slower service simply
receives less work. It can never show a queue forming. That is also why it
declares no `min_rps` — see below.

The open profile offers requests on a schedule regardless of whether earlier ones
came back, capped at `max_vus: 60`. By Little's Law the fleet sustains 200/s only
while `200 × latency ≤ 60`, so it keeps up to ~300ms and sheds after that —
*before* p95 crosses 400ms. Throughput becomes a leading indicator rather than a
restatement of the latency gate.

### A threshold that could not fire

This repo shipped one, and `tools/prove_reachable.py` exists because of it.

The closed profile originally carried `min_rps: 15` next to `p95_ms: 400`. In a
closed model throughput is `vus / (think + latency)` — a property of the *load
generator*:

| condition | latency | throughput |
|---|---|---|
| clean run | 37ms | 59.3/s |
| at the p95 threshold | 400ms | 28.6/s |
| under the 600ms injected fault | 744ms | 19.2/s |
| **`min_rps: 15` needs** | **>1033ms** | <15/s |

p95 fails at 400ms. Throughput could not breach until 1033ms — p95 had already
failed by **2.6×**. It never fired in any run, including under fault, and it never
could have. `make prove-teeth` did not catch it, because proving the verdict can
flip says nothing about whether each individual threshold is reachable.

`prove_reachable.py` now walks latency upward for every profile and fails the build
if any threshold is dominated by another. It runs in `make test` — no Docker, no
load generator, milliseconds — and it keeps the config that shipped the bug as a
regression fixture:

```
$ make test
>> reachability self-test
self-test OK
  the config this repo shipped is correctly rejected:
    profile 'historical': min_rps=15/s is dominated by p95=400ms. Throughput cannot
    breach until latency reaches 1034ms, by which point p95 has already failed by
    2.6x. The threshold cannot fire on its own and is decoration.
```

---

## `make prove-divergence` — proving the *other* gate can fail

`prove-teeth` proves the SLO thresholds bite. It says nothing about the divergence
bands, which are the part that decides whether a green run is trustworthy at all.

So: run JMeter as configured and k6 at **2× the load**, against a perfectly healthy
service. Both engines comfortably pass their own SLO — that is the whole point. A
build that only checked thresholds would wave this through.

```
$ make prove-divergence
  rps  [gated]   jmeter=     63.66/s   k6=    125.75/s   gap=  49.4%

  jmeter verdict: PASS
  k6 verdict: PASS

  RESULT: PASS -- the gate rejected a desynchronised run:
    - throughput divergence 49.4% exceeds the 10% band (63.66/s vs 125.75/s)
```

Both engines green, build correctly red. If the bands had *not* noticed, that is
reported as a failure of its own — a divergence check too loose to detect a
deliberate 2× desync is decoration, and the target says so in those words.

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

This runs in CI on every push as part of a **required status check**, not as a step
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
slo.json                    single source of truth: profiles, thresholds, bands
docker-compose.yml          postgres + postgrest + toxiproxy
Makefile                    up / load / prove-teeth / report / down

jmeter/workload.jmx         plan shape only; every value is ${__P(...)}; both models
jmeter/user.properties      lean .jtl output (no response bodies during measurement)
k6/workload.js              reads slo.json via open(); both models; no literals

tools/slo.py                config loader, profile resolution, weight bands
tools/gen_jmeter_props.py   slo.json  ->  JMeter .properties
tools/summarize.py          THE shared reduction, + --self-test
tools/cross_verify.py       THE gate: SLO + agreement + divergence
tools/prove_reachable.py    fails the build on a threshold that cannot fire
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

**Why is no latency percentile in the divergence band?** Because latency was
measured against a known-sabotaged run and could not tell it from an honest one.
The table under *Disagreement is a failure* has the numbers. The underlying reason
is that JMeter stores whole-millisecond elapsed times and k6 keeps sub-millisecond
floats, so the gap is a fixed absolute error whose *percentage* swings with
wherever the statistic happens to land — 39.5%, 14.7% and 1.9% on three p99
samples across otherwise comparable runs. Throughput and scenario mix have no such
sensitivity.

**Why gate the open profile on throughput at all, if both engines are pinned to
the same arrival rate?** Because they are only pinned while the service keeps up.
`max_vus` is set to 60 deliberately: by Little's Law the fleet sustains 200/s only
while `200 * latency <= 60`, so it holds up to ~300ms and starts shedding after
that — *before* p95 crosses 400ms. Throughput is a leading indicator in that
profile rather than a lagging restatement of the latency gate, which is exactly
what `tools/prove_reachable.py` enforces.

**Why does `make prove-teeth` run only the closed profile?** The injected fault is
600ms of latency, which trips the SLO in either model. Running both would double
the runtime to demonstrate the same thing twice.

---

<p align="center"><sub>
Diego Navarro Ruiz · QA Automation Engineer ·
<a href="https://github.com/Diegonava150">github.com/Diegonava150</a>
</sub></p>
