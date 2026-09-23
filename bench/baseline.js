// Baseline: what the gateway itself costs.
//
// Every prompt is unique, so the exact cache misses on every request and the
// mock provider answers each one from a derived reply with no network and no
// sleep. What is left in the measurement is *our* code and nothing else: the
// two raw-ASGI middlewares, auth, admission against the token bucket, a cache
// lookup that hashes the request and misses in Redis, contract validation, and
// the response serialisation.
//
// This is the denominator for every other number in bench/. A cache hit is only
// worth quoting as "N times cheaper than a provider call" against a figure that
// says what a request costs when nothing goes right and nothing goes wrong.
//
//   k6 run bench/baseline.js
//   BENCH_MODE=rate BENCH_RATE=2000 k6 run bench/baseline.js
//
// See bench/README.md for the environment and the matrix runner.

import http from 'k6/http';
import {
  BASE_URL,
  CHAT_PATH,
  JSON_HEADERS,
  MOCK_MODEL,
  ONLY_2XX,
  TREND_STATS,
  body,
  checkCompletion,
  recordCache,
  recordStatus,
  scenario,
  summary,
  uniquePrompt,
} from './lib/common.js';

export const options = {
  scenarios: scenario('baseline'),
  summaryTrendStats: TREND_STATS,
  thresholds: {
    // A failure here is a gateway that broke under load, not a slow one. Kept
    // tight on purpose: the mock provider cannot fail, so anything non-2xx on
    // this workload is the gateway refusing its own traffic.
    http_req_failed: ['rate<0.005'],
    checks: ['rate>0.995'],
    // Not a service objective — a regression tripwire. The number is the one
    // the first committed run measured, rounded up; see bench/README.md.
    http_req_duration: ['p(95)<250', 'p(99)<500'],
  },
  discardResponseBodies: false,
};

export default function () {
  const res = http.post(BASE_URL + CHAT_PATH, body(MOCK_MODEL, uniquePrompt()), {
    headers: JSON_HEADERS,
    tags: { workload: 'baseline' },
    responseCallback: ONLY_2XX,
  });

  checkCompletion(res);
  recordCache(res);
  recordStatus(res, 'baseline');
}

export function handleSummary(data) {
  return summary(data, __ENV.BENCH_LABEL || 'baseline');
}
