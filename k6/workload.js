// k6 half of the pair.
//
// Every number in this file comes from ../slo.json. There are no literals for
// VUs, duration, ramp, think time, endpoints, weights or thresholds -- if you
// find yourself wanting to type one, put it in slo.json so the JMeter plan gets
// it too. That is the whole discipline: two engines, one definition.
//
// `open()` runs in init context, so the config is read once per VU at startup
// and never during the test.

import http from 'k6/http';
import { sleep } from 'k6';

const cfg = JSON.parse(open('../slo.json'));
const W = cfg.workload;

// Cumulative weight bands, built exactly as tools/slo.py builds them for JMeter.
const SCENARIOS = (() => {
  let running = 0;
  return cfg.scenarios.map((s) => {
    running += s.weight;
    return { ...s, cum: running };
  });
})();

export const options = {
  stages: [
    { duration: `${W.ramp_up_s}s`, target: W.vus },
    { duration: `${W.duration_s}s`, target: W.vus },
    { duration: '5s', target: 0 },
  ],

  // k6's native opinion of the run. The build is NOT gated on these --
  // tools/cross_verify.py scores k6 and JMeter with the same Python summarizer
  // so the comparison is fair. These are here because they are the idiomatic
  // way to read a k6 run, and because when they disagree with the shared
  // summarizer that disagreement is itself worth seeing in the log.
  //
  // Scoped to {phase:steady} so k6's own thresholds ignore the ramp, matching
  // the trim the shared summarizer applies to both engines.
  thresholds: {
    'http_req_duration{phase:steady}': [
      `p(95)<${cfg.slo.p95_ms}`,
      `p(99)<${cfg.slo.p99_ms}`,
    ],
    'http_req_failed{phase:steady}': [`rate<${cfg.slo.error_rate}`],
  },

  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
  discardResponseBodies: true,   // we are measuring the service, not the parser
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

  // Phase tag drives k6's native thresholds only. The shared summarizer does
  // its own trim from the raw JSON stream, so the two never depend on each other.
  const phase = Date.now() - data.t0 < W.ramp_up_s * 1000 ? 'warmup' : 'steady';

  http.get(`${cfg.base_url}${s.path}`, {
    tags: { phase, scenario: s.name },
  });

  sleep(W.think_time_ms / 1000);
}
