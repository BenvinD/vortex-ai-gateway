// Provider failure: the breaker, under load, with a bystander watching.
//
// Day 5 argued that a circuit breaker turns a retry storm into a refusal that
// costs nothing, and Day 12 showed it as six upstream attempts followed by zero
// (docs/notes/day-12.md). Both of those were four requests sent by hand. Under
// load the claim has a second half nobody has tested: a gateway whose upstream
// is dead is a gateway spending its event loop on connections that will not
// complete, and the requests that suffer for it are the ones that never needed
// that provider at all.
//
// So this script runs two scenarios against one gateway at once:
//
//   * **storm** — an open-loop arrival rate at a model routed to a dead
//     upstream. Open loop deliberately: closed-loop VUs would slow down to the
//     rate the failures return at, which is the retry storm declining to happen.
//   * **bystander** — a low, steady rate of `GET /v1/usage`, which is
//     authenticated, touches Redis, and never touches a provider. Its p99 is
//     the measurement that matters here. If it degrades in step with the storm,
//     a single dead vendor is a whole-gateway outage and the breaker is not
//     buying what ADR-002 says it buys.
//
// The three statuses are the story, and which one you get says which layer
// answered:
//
//   502  the retry loop ran and gave up   — this cost `retry_max_attempts` calls
//   504  the upstream timed out            — ditto, at connect-timeout each
//   503  the breaker refused               — this cost nothing, and is the point
//
// **Pick the dead upstream deliberately**, because the two obvious choices
// measure different things:
//
//   VORTEX_OPENAI_BASE_URL=http://127.0.0.1:9        connection refused,
//       so each attempt fails in microseconds. This measures the *overhead* of
//       the retry loop and the breaker bookkeeping, and the 502s will be fast.
//   VORTEX_OPENAI_BASE_URL=http://10.255.255.1:1     packets blackholed,
//       so each attempt costs `VORTEX_CONNECT_TIMEOUT_SECONDS`. This measures
//       what the breaker is actually for: three attempts at 5s each, held open
//       for the duration, against a 503 that returns immediately.
//
// The matrix runner uses the blackholed address, because the refused one makes
// the breaker look unnecessary.
//
//   k6 run bench/provider-failure.js

import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';
import {
  BASE_URL,
  CHAT_PATH,
  DURATION,
  JSON_HEADERS,
  RATE,
  TREND_STATS,
  USAGE_PATH,
  VUS,
  body,
  recordStatus,
  summary,
  uniquePrompt,
} from './lib/common.js';

// Whatever the routing table points at the dead upstream. `run-matrix.sh` sets
// VORTEX_MODEL_ROUTES=broken-*=openai and gives openai a blackholed base URL.
const BROKEN_MODEL = __ENV.BENCH_BROKEN_MODEL || 'broken-model';

// The offered rate on the failing path. Low by default compared with the other
// workloads: each request may hold a connection for three connect timeouts, so
// a high rate here is measuring how many sockets the machine has rather than
// anything about the gateway.
const STORM_RATE = Number(__ENV.BENCH_STORM_RATE || Math.max(10, Math.floor(RATE / 20)));
const BYSTANDER_RATE = Number(__ENV.BENCH_BYSTANDER_RATE || 20);

// Split out from http_req_duration because the whole finding is the *gap*
// between them: a refusal that is not dramatically cheaper than a give-up means
// the breaker is not paying for itself.
const gaveUp = new Trend('vortex_gave_up_ms', true);
const refused = new Trend('vortex_refused_ms', true);
const bystanderLatency = new Trend('vortex_bystander_ms', true);

export const options = {
  scenarios: {
    storm: {
      executor: 'constant-arrival-rate',
      exec: 'storm',
      rate: STORM_RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(VUS, STORM_RATE * 2),
      maxVUs: Math.max(VUS * 4, STORM_RATE * 8),
      gracefulStop: '15s',
    },
    bystander: {
      executor: 'constant-arrival-rate',
      exec: 'bystander',
      rate: BYSTANDER_RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 8,
      maxVUs: 64,
      gracefulStop: '15s',
    },
  },
  summaryTrendStats: TREND_STATS,
  thresholds: {
    // The bystander is the assertion. Everything else on this workload is
    // expected to fail, and a threshold that lumped them together would be a
    // test that can only ever be red.
    'vortex_bystander_ms': ['p(95)<250', 'p(99)<1000'],
    'checks{scenario:bystander}': ['rate>0.99'],
    // Not asserted, reported: how the storm was answered is the finding, and a
    // threshold on it would encode today's breaker settings as a requirement.
  },
};

// The default callback marks every 5xx as a failed request, which would make
// `http_req_failed` on this workload a constant 1.0 carrying no information.
// Here a 502, a 503 and a 504 are expected answers; anything else is not. Set in
// the init context, because k6 does not read `responseCallback` out of
// `options` — it warns "unknown field" and ignores it, which is how the first
// published run counted the whole storm as failures.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 299 }, 502, 503, 504));

export function storm() {
  const res = http.post(BASE_URL + CHAT_PATH, body(BROKEN_MODEL, uniquePrompt()), {
    headers: JSON_HEADERS,
    tags: { workload: 'provider-failure' },
  });

  check(res, {
    'answered, not hung': (r) => r.status !== 0,
    'failure is one of the mapped statuses': (r) =>
      r.status === 502 || r.status === 503 || r.status === 504,
    // A refusal the gateway makes about *itself* carries Retry-After, because
    // no call was made and the caller can be told exactly when to come back.
    'a 503 carries Retry-After': (r) =>
      r.status !== 503 || r.headers['Retry-After'] !== undefined,
    // The envelope survives the failure path: a caller gets a typed error, not
    // a bare status (ADR-010, ADR-015).
    'error envelope is typed': (r) => {
      if (r.status < 400) return true;
      try {
        return typeof r.json().error.type === 'string';
      } catch (e) {
        return false;
      }
    },
  });

  if (res.status === 503) {
    refused.add(res.timings.duration);
  } else if (res.status === 502 || res.status === 504) {
    gaveUp.add(res.timings.duration);
  }
  recordStatus(res, 'storm');
}

export function bystander() {
  // Authenticated, reads Redis, touches no provider. The one request in this
  // file that is supposed to succeed.
  const res = http.get(`${BASE_URL}${USAGE_PATH}?days=1`, {
    headers: JSON_HEADERS,
    tags: { workload: 'bystander' },
  });

  check(res, {
    'bystander is 200': (r) => r.status === 200,
    'bystander got a report': (r) => {
      if (r.status !== 200) return false;
      try {
        return typeof r.json().key_id === 'string';
      } catch (e) {
        return false;
      }
    },
  });

  bystanderLatency.add(res.timings.duration);
  recordStatus(res, 'bystander');
}

export function handleSummary(data) {
  return summary(data, __ENV.BENCH_LABEL || 'provider-failure');
}
