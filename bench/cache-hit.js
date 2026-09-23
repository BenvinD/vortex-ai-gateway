// Cache hit: what the exact tier actually saves.
//
// A small prompt pool, warmed once in `setup()` so the measured window is
// almost entirely hits rather than the transient that fills the cache. The
// number this workload exists for is the **p99 of a HIT**, not the mean: a
// cache whose median is a hash and whose tail is a Redis timeout has not
// removed a provider call, it has moved the variance.
//
// With BENCH_SEMANTIC=on the pool is sent as *paraphrases* instead. The exact
// tier then cannot match them and the semantic tier is supposed to, which makes
// this the same script measuring the other tier — embed, cosine against the
// stored unit vectors, threshold — and the cost of the promotion into the exact
// tier that a semantic hit performs (ADR-024).
//
// Two caveats that change how the result reads, both from AGENTS.md:
//
//   * The exact tier lives in Redis and is shared across workers. The semantic
//     index is **per worker process**. With `--workers 4` a warmup request
//     lands on one worker, so the other three start cold and the measured
//     semantic hit rate is bounded by roughly 1/workers until every process has
//     seen every prompt. The matrix runner warms per-worker-count for that
//     reason and the README table says so.
//   * A hit is settled at zero tokens, so this workload moves the request
//     counters and not the token or dollar ones.
//
//   k6 run bench/cache-hit.js
//   BENCH_SEMANTIC=on k6 run bench/cache-hit.js

import http from 'k6/http';
import {
  BASE_URL,
  CHAT_PATH,
  JSON_HEADERS,
  MOCK_MODEL,
  ONLY_2XX,
  PROMPT_POOL,
  TREND_STATS,
  body,
  checkCompletion,
  paraphrase,
  poolPrompt,
  recordCache,
  recordStatus,
  scenario,
  summary,
} from './lib/common.js';

const SEMANTIC = (__ENV.BENCH_SEMANTIC || 'off') === 'on';

// How many times over to warm each prompt. More than once because with N
// uvicorn workers the first send populates Redis (shared) but only one
// worker's semantic index (not shared), and the semantic arm needs every
// worker warm before the measured window starts.
const WARM_PASSES = Number(__ENV.BENCH_WARM_PASSES || 8);

export const options = {
  scenarios: scenario('cache_hit'),
  summaryTrendStats: TREND_STATS,
  thresholds: {
    http_req_failed: ['rate<0.005'],
    checks: ['rate>0.995'],
    // The claim the tier makes. A warmed exact cache that is not comfortably
    // faster than the baseline is a tier that is not earning its Redis.
    http_req_duration: ['p(95)<250', 'p(99)<500'],
    // Deliberately a threshold and not just a reported number: a run whose hit
    // rate collapsed measured something other than what this file says it
    // measures, and it should fail rather than publish a fast p99 for a
    // workload that was quietly all misses.
    'vortex_cache_hit{tier:exact}': SEMANTIC ? [] : ['rate>0.90'],
  },
};

export function setup() {
  // Sent serially, ignoring the timings: this is the transient, and including
  // it in the measurement would report the cost of filling the cache as the
  // cost of using it.
  for (let pass = 0; pass < WARM_PASSES; pass++) {
    for (let i = 0; i < PROMPT_POOL.length; i++) {
      http.post(BASE_URL + CHAT_PATH, body(MOCK_MODEL, poolPrompt(i)), {
        headers: JSON_HEADERS,
        tags: { workload: 'warmup' },
        responseCallback: ONLY_2XX,
      });
    }
  }
  return { warmed: PROMPT_POOL.length, passes: WARM_PASSES };
}

export default function () {
  // Round-robin rather than random, so two runs of the same arm send the same
  // sequence and the hit rate is a property of the cache and not of a seed.
  const n = __ITER * 7 + __VU;
  const prompt = SEMANTIC ? paraphrase(n) : poolPrompt(n);

  const res = http.post(BASE_URL + CHAT_PATH, body(MOCK_MODEL, prompt), {
    headers: JSON_HEADERS,
    tags: { workload: SEMANTIC ? 'semantic-hit' : 'cache-hit' },
    responseCallback: ONLY_2XX,
  });

  checkCompletion(res);
  recordCache(res);
  recordStatus(res, SEMANTIC ? 'semantic-hit' : 'cache-hit');
}

export function handleSummary(data) {
  return summary(data, __ENV.BENCH_LABEL || (SEMANTIC ? 'semantic-hit' : 'cache-hit'));
}
