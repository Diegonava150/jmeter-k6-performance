// k6 half of the pair.
//
// Every number in this file comes from ../slo.json. There are no literals for
// VUs, arrival rate, duration, ramp, think time, endpoints, weights or
// thresholds -- if you find yourself wanting to type one, put it in slo.json so
// the JMeter plan gets it too. That is the whole discipline: two engines, one
// definition.
//
// `open()` runs in init context, so the config is read once per VU at startup
// and never during the test.
//
// Env overrides, both used by the Makefile and neither by a normal run:
//   PROFILE=closed|open   which workload model to run
//   LOAD_MULTIPLIER=2     deliberately desynchronise this engine from JMeter,
//                         so `make prove-divergence` can check the gate notices

import http from 'k6/http';
import { sleep } from 'k6';

const cfg = JSON.parse(open('../slo.json'));

const PROFILE = __ENV.PROFILE || 'closed';
const MULT = parseFloat(__ENV.LOAD_MULTIPLIER || '1');

const prof = cfg.profiles[PROFILE];
if (!prof) {
  throw new Error(
    `unknown profile '${PROFILE}'; slo.json defines ${Object.keys(cfg.profiles)}`
  );
}

// Cumulative weight bands, built exactly as tools/slo.py builds them for JMeter.
const SCENARIOS = (() => {
  let running = 0;
  return cfg.scenarios.map((s) => {
    running += s.weight;
    return { ...s, cum: running };
  });
})();

// Closed model: hold concurrency, let think time pace each VU, and accept that
// the offered load backs off when the service slows.
// Open model: hold the ARRIVAL RATE, let concurrency grow to whatever the rate
// demands up to max_vus, and let a slow service build a real queue.
function buildScenario() {
  if (prof.model === 'closed') {
    const target = Math.max(1, Math.round(prof.vus * MULT));
    return {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: `${prof.ramp_up_s}s`, target },
        { duration: `${prof.duration_s}s`, target },
        { duration: '5s', target: 0 },
      ],
      gracefulRampDown: '5s',
    };
  }
  const target = Math.max(1, Math.round(prof.rate_per_s * MULT));
  const vus = Math.max(1, Math.round(prof.max_vus * MULT));
  return {
    executor: 'ramping-arrival-rate',
    startRate: 0,
    timeUnit: '1s',
    preAllocatedVUs: vus,
    // The cap is the point, not a safety valve: once rate * latency exceeds it
    // the fleet cannot keep up and achieved throughput falls below target, which
    // is what makes min_rps a real signal in this profile.
    maxVUs: vus,
    stages: [
      { duration: `${prof.ramp_up_s}s`, target },
      { duration: `${prof.duration_s}s`, target },
    ],
  };
}

// k6's native opinion of the run. The build is NOT gated on these --
// tools/cross_verify.py scores k6 and JMeter with the same Python summarizer so
// the comparison is fair. These are here because they are the idiomatic way to
// read a k6 run, and because when they disagree with the shared summarizer that
// disagreement is itself worth seeing in the log.
//
// Scoped to {phase:steady} so k6's own thresholds ignore the ramp, matching the
// trim the shared summarizer applies to both engines. Throughput is deliberately
// absent: k6 computes a rate over total test time including the ramp, which is
// not the same window the summarizer scores, so min_rps is left to the one place
// that applies it identically to both engines.
function buildThresholds() {
  return {
    'http_req_duration{phase:steady}': [
      `p(95)<${prof.slo.p95_ms}`,
      `p(99)<${prof.slo.p99_ms}`,
    ],
    'http_req_failed{phase:steady}': [`rate<${prof.slo.error_rate}`],
  };
}

export const options = {
  scenarios: { main: buildScenario() },
  thresholds: buildThresholds(),
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
  // Deliberately false. JMeter always reads the response body, so discarding it
  // here would make k6 do measurably less work per request than the engine it is
  // being compared against -- a mismatch in the one thing this repo exists to
  // detect. Symmetry beats a slightly cheaper load generator.
  discardResponseBodies: false,
  noConnectionReuse: false,
};

export function setup() {
  const probe = http.get(`${cfg.base_url}${cfg.scenarios[0].path}`);
  if (probe.status !== 200) {
    throw new Error(
      `stack not ready: GET ${cfg.scenarios[0].path} returned ${probe.status}. ` +
      `Run \`make up\` first.`
    );
  }
  return { t0: Date.now() };
}

function pick() {
  const roll = Math.random() * 100;
  for (const s of SCENARIOS) {
    if (roll < s.cum) return s;
  }
  return SCENARIOS[SCENARIOS.length - 1];
}

export default function (data) {
  const s = pick();

  // Phase tag drives k6's native thresholds only. The shared summarizer does its
  // own trim from the raw JSON stream, so the two never depend on each other.
  const phase = Date.now() - data.t0 < prof.ramp_up_s * 1000 ? 'warmup' : 'steady';

  http.get(`${cfg.base_url}${s.path}`, {
    tags: { phase, scenario: s.name },
  });

  // Zero in the open profile: arrival pacing is the executor's job there, and a
  // think time on top would silently turn it back into a closed model.
  if (prof.think_time_ms > 0) sleep(prof.think_time_ms / 1000);
}
