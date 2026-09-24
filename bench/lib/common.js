// Shared rig for every script in bench/.
//
// Three things live here rather than in each workload, because a load suite
// whose scripts disagree about concurrency, about what counts as a failure, or
// about how a percentile is named is a suite whose runs cannot be compared:
//
//   * `scenario()`  — one concurrency model, chosen by BENCH_MODE, for all four.
//   * `summary()`   — one machine-readable result file per run, one shape.
//   * `recordCache()` — the cache outcome read off the response headers, which
//                       is the same place `middleware.py` reads it (ADR-026).
//
// Nothing here imports from jslib.k6.io or anywhere else on the network. A
// benchmark that needs the internet to start is one that cannot be run on the
// machine it is measuring when that machine is the interesting case.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

export const BASE_URL = __ENV.BENCH_URL || 'http://127.0.0.1:8000';
export const CHAT_PATH = '/v1/chat/completions';
export const USAGE_PATH = '/v1/usage';

// Any well-formed bearer token is accepted while VORTEX_API_KEYS is empty,
// which is how `run-matrix.sh` starts the gateway. One key for the whole run on
// purpose: the rate limiter, the ledger and the cache are all namespaced by
// key_id, so a per-VU key would give every VU its own cache and measure a hit
// rate of zero (ADR-003, ADR-004).
export const API_KEY = __ENV.BENCH_API_KEY || 'bench-key';

// The model the mock provider answers for. With no routing table configured
// every model name lands on the mock, so this is a label, not a route.
export const MOCK_MODEL = __ENV.BENCH_MODEL || 'gpt-4o-mini';

export const JSON_HEADERS = {
  Authorization: `Bearer ${API_KEY}`,
  'Content-Type': 'application/json',
};

// --- concurrency -----------------------------------------------------------

// Closed loop by default. `BENCH_MODE=rate` switches to an open loop, and the
// two answer different questions: a closed-loop run reports latency at a known
// concurrency and cannot produce a queue, an open-loop run holds the request
// rate fixed whether or not the gateway keeps up and is the only one of the two
// that can show an error rate under saturation. See docs/design/day-13.md.
export const MODE = __ENV.BENCH_MODE || 'vus';
export const VUS = Number(__ENV.BENCH_VUS || 32);
export const RATE = Number(__ENV.BENCH_RATE || 1000);
export const DURATION = __ENV.BENCH_DURATION || '30s';
export const WARMUP = __ENV.BENCH_WARMUP || '5s';

export function scenario(name) {
  if (MODE === 'rate') {
    return {
      [name]: {
        executor: 'constant-arrival-rate',
        rate: RATE,
        timeUnit: '1s',
        duration: DURATION,
        // Enough VUs that the executor is never the thing limiting the rate;
        // k6 warns loudly if it cannot keep up, and that warning is a result,
        // not a problem with the script.
        preAllocatedVUs: Math.max(VUS, Math.ceil(RATE / 4)),
        maxVUs: Math.max(VUS, RATE),
        gracefulStop: '10s',
      },
    };
  }
  return {
    [name]: {
      executor: 'constant-vus',
      vus: VUS,
      duration: DURATION,
      gracefulStop: '10s',
    },
  };
}

// Reported for every trend metric, so p99 exists in the JSON rather than being
// interpolated from p95 afterwards. k6 only computes the statistics named here.
export const TREND_STATS = ['min', 'avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'];

// --- what each workload measures beyond the HTTP timings -------------------

export const cacheHits = new Rate('vortex_cache_hit');
export const cacheOutcomes = new Counter('vortex_cache_outcome');
export const semanticScore = new Trend('vortex_semantic_score');
export const ttft = new Trend('vortex_ttft_ms', true);
export const streamBytes = new Trend('vortex_stream_bytes');
export const statusCodes = new Counter('vortex_status');

// The tier outcomes, read off the response headers exactly as the access log
// and the Prometheus middleware read them. A response with no X-Cache header
// is a deployment with no cache, and it contributes to neither half of the hit
// rate — the same rule `_cache_field` follows in middleware.py.
export function recordCache(res) {
  const exact = res.headers['X-Cache'];
  const semantic = res.headers['X-Semantic-Cache'];
  const score = res.headers['X-Semantic-Cache-Score'];

  if (exact !== undefined) {
    cacheOutcomes.add(1, { tier: 'exact', outcome: exact });
    // BYPASS is in neither half: the cache was never asked, so it cannot have
    // missed (ADR-004).
    if (exact === 'HIT' || exact === 'MISS') {
      cacheHits.add(exact === 'HIT', { tier: 'exact' });
    }
  }
  if (semantic !== undefined) {
    cacheOutcomes.add(1, { tier: 'semantic', outcome: semantic });
    if (semantic === 'HIT' || semantic === 'MISS') {
      cacheHits.add(semantic === 'HIT', { tier: 'semantic' });
    }
  }
  // Recorded on misses too. That distribution is how ADR-005's threshold gets
  // tuned, and a run that keeps only the hits cannot tell a threshold that is
  // too high from a corpus with no paraphrases in it.
  if (score !== undefined) {
    semanticScore.add(Number(score));
  }
}

export function recordStatus(res, kind) {
  statusCodes.add(1, { status: String(res.status), kind: kind });
}

// --- request bodies --------------------------------------------------------

export function body(model, prompt, extra) {
  return JSON.stringify(
    Object.assign(
      {
        model: model,
        messages: [{ role: 'user', content: prompt }],
        max_completion_tokens: 64,
      },
      extra || {}
    )
  );
}

// Unique per virtual user per iteration, so the exact cache misses every time
// and the baseline measures the gateway rather than Redis. Deliberately not
// random: a run is reproducible, and two runs of the same arm send the same
// bytes.
export function uniquePrompt() {
  return `bench u${__VU} i${__ITER} — summarise the CAP theorem in one sentence.`;
}

// A small fixed pool, which is what produces hits. The pool size is the knob:
// N distinct prompts against a warmed cache is a hit rate of (1 - N/requests),
// and the default is small enough that the steady state is nearly all hits.
export const PROMPT_POOL = [
  'What is the capital of France?',
  'Explain a circuit breaker in one paragraph.',
  'How do I reverse a list in Python?',
  'Summarise the CAP theorem.',
  'Why is the sky blue?',
  'What does HTTP 429 mean?',
  'Write a haiku about latency.',
  'Difference between a mutex and a semaphore?',
];

export function poolPrompt(n) {
  return PROMPT_POOL[n % PROMPT_POOL.length];
}

// Paraphrases of the pool, for the semantic arm: the exact tier cannot match
// these and the semantic tier is supposed to. Same order as PROMPT_POOL so
// index N is a paraphrase of prompt N.
export const PARAPHRASE_POOL = [
  'Which city is the capital of France?',
  'In one paragraph, what does a circuit breaker do?',
  'What is the way to reverse a list in Python?',
  'Give me a summary of the CAP theorem.',
  'What makes the sky look blue?',
  'What is the meaning of HTTP status 429?',
  'Compose a haiku on the subject of latency.',
  'How does a mutex differ from a semaphore?',
];

export function paraphrase(n) {
  return PARAPHRASE_POOL[n % PARAPHRASE_POOL.length];
}

// --- shared checks ---------------------------------------------------------

export function checkCompletion(res) {
  return check(res, {
    'status is 200': (r) => r.status === 200,
    'body is a completion': (r) => {
      if (r.status !== 200) return false;
      try {
        const json = r.json();
        return json.object === 'chat.completion' && json.choices.length > 0;
      } catch (e) {
        return false;
      }
    },
    'usage is reported': (r) => {
      if (r.status !== 200) return false;
      try {
        return r.json().usage.total_tokens > 0;
      } catch (e) {
        return false;
      }
    },
  });
}

// --- results ---------------------------------------------------------------

// One JSON file per run plus a compact stdout block carrying exactly the
// columns the README table has. Defining handleSummary replaces k6's own
// stdout summary, so this prints its own rather than importing k6-summary from
// the network.
export function summary(data, label) {
  const name = label || __ENV.BENCH_LABEL || 'run';
  const dir = __ENV.BENCH_OUT || 'bench/results';
  const out = {};
  out[`${dir}/${name}.json`] = JSON.stringify(data, null, 2);
  out.stdout = renderSummary(data, name);
  return out;
}

function stat(data, metric, key) {
  const m = data.metrics[metric];
  if (m === undefined || m.values === undefined) return null;
  const value = m.values[key];
  return value === undefined ? null : value;
}

function ms(value) {
  return value === null ? '   —  ' : `${value.toFixed(2)}ms`;
}

function pct(value) {
  return value === null ? '  — ' : `${(value * 100).toFixed(2)}%`;
}

function renderSummary(data, name) {
  const lines = [];
  const seconds = (data.state.testRunDurationMs || 0) / 1000;
  lines.push('');
  lines.push(`  ${name}  —  ${MODE} mode, ${MODE === 'rate' ? `${RATE}/s target` : `${VUS} VUs`}, ${seconds.toFixed(1)}s`);
  lines.push('');
  lines.push(`    requests    ${stat(data, 'http_reqs', 'count') || 0} at ${(stat(data, 'http_reqs', 'rate') || 0).toFixed(1)}/s`);
  lines.push(`    latency     p50=${ms(stat(data, 'http_req_duration', 'med'))}  p95=${ms(stat(data, 'http_req_duration', 'p(95)'))}  p99=${ms(stat(data, 'http_req_duration', 'p(99)'))}  max=${ms(stat(data, 'http_req_duration', 'max'))}`);
  lines.push(`    errors      ${pct(stat(data, 'http_req_failed', 'rate'))} of requests`);
  lines.push(`    checks      ${pct(stat(data, 'checks', 'rate'))} passed`);

  const hit = stat(data, 'vortex_cache_hit', 'rate');
  if (hit !== null) {
    lines.push(`    cache       ${pct(hit)} hit rate (bypasses excluded)`);
  }
  const first = stat(data, 'vortex_ttft_ms', 'p(95)');
  if (first !== null) {
    lines.push(`    ttft        p50=${ms(stat(data, 'vortex_ttft_ms', 'med'))}  p95=${ms(first)}  p99=${ms(stat(data, 'vortex_ttft_ms', 'p(99)'))}`);
  }
  const score = stat(data, 'vortex_semantic_score', 'med');
  if (score !== null) {
    lines.push(`    similarity  p50=${score.toFixed(4)}  p95=${(stat(data, 'vortex_semantic_score', 'p(95)') || 0).toFixed(4)}`);
  }

  const failed = Object.keys(data.metrics)
    .filter((k) => k === 'vortex_status')
    .length;
  if (failed) {
    lines.push('');
    lines.push('    (status breakdown is in the JSON, under vortex_status submetrics)');
  }
  lines.push('');
  return lines.join('\n');
}

// A default `responseCallback` that treats only 2xx as success. Overridden in
// provider-failure.js, where a 502 and a 503 are the measurement rather than a
// problem with the run.
export const ONLY_2XX = http.expectedStatuses({ min: 200, max: 299 });
