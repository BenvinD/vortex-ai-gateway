// Streaming: the other half of the gateway's response path.
//
// A streamed response leaves through code the buffered path never touches —
// `StreamingResponse`, the per-chunk accounting in `streaming.py`, the usage
// chunk that is always requested and conditionally stripped (ADR-018), and a
// settlement that runs in a `finally` — so a suite that measures only buffered
// requests has measured half the service.
//
// **Time to first token.** k6 cannot read an SSE body incrementally without the
// `xk6-sse` extension, which needs a custom binary and so cannot be a script
// anybody can run. What it *can* do is report `timings.waiting`, which is time
// to first byte, and for a `text/event-stream` response the first byte is the
// first flushed chunk. That is a client-side TTFT, and it is deliberately
// recorded here next to the gateway's own `vortex_stream_ttft_seconds`
// histogram, which the matrix runner scrapes: two independent measurements of
// one quantity, where a disagreement is a bug in the instrument rather than a
// fact about the service (see docs/notes/day-12.md on why that matters).
//
// Half the iterations ask for usage and half do not, so both sides of ADR-018
// are exercised: the gateway asks every provider for usage regardless, and
// strips the usage chunk back out when the caller did not want it.
//
//   k6 run bench/streaming.js

import http from 'k6/http';
import { check } from 'k6';
import {
  BASE_URL,
  CHAT_PATH,
  JSON_HEADERS,
  MOCK_MODEL,
  ONLY_2XX,
  TREND_STATS,
  body,
  recordCache,
  recordStatus,
  scenario,
  streamBytes,
  summary,
  ttft,
  uniquePrompt,
} from './lib/common.js';

export const options = {
  scenarios: scenario('streaming'),
  summaryTrendStats: TREND_STATS,
  thresholds: {
    http_req_failed: ['rate<0.005'],
    checks: ['rate>0.995'],
    // A stream's total duration is one token per chunk through the mock, so it
    // is legitimately longer than a buffered reply and the tripwire is looser.
    http_req_duration: ['p(95)<500', 'p(99)<1000'],
    // The number a streaming client actually feels.
    vortex_ttft_ms: ['p(95)<250', 'p(99)<500'],
  },
};

export default function () {
  // include_usage on alternating iterations, which is what puts both the
  // forwarded and the stripped usage chunk in one run.
  const wantsUsage = __ITER % 2 === 0;
  const extra = { stream: true };
  if (wantsUsage) {
    extra.stream_options = { include_usage: true };
  }

  const res = http.post(BASE_URL + CHAT_PATH, body(MOCK_MODEL, uniquePrompt(), extra), {
    headers: JSON_HEADERS,
    tags: { workload: 'streaming', usage: wantsUsage ? 'included' : 'stripped' },
    responseCallback: ONLY_2XX,
  });

  check(res, {
    'status is 200': (r) => r.status === 200,
    'content type is SSE': (r) =>
      (r.headers['Content-Type'] || '').indexOf('text/event-stream') === 0,
    // The terminator, not the connection closing: an OpenAI-compatible client
    // stops reading on this and a stream that omitted it would look complete
    // to a byte counter and truncated to a client (ADR-012).
    'stream ends with [DONE]': (r) => r.body.indexOf('data: [DONE]') !== -1,
    'stream carries deltas': (r) => r.body.indexOf('"delta"') !== -1,
    // Streams bypass the cache in both directions, and are reported BYPASS
    // rather than MISS because the cache was never asked (ADR-004).
    'cache reports BYPASS or is absent': (r) => {
      const outcome = r.headers['X-Cache'];
      return outcome === undefined || outcome === 'BYPASS';
    },
    // The half that asked gets a usage chunk; the half that did not must not,
    // even though the gateway asked the provider for one either way.
    'usage chunk matches what was asked': (r) => {
      const present = r.body.indexOf('"usage"') !== -1;
      return wantsUsage ? present : !present;
    },
  });

  ttft.add(res.timings.waiting);
  streamBytes.add(res.body.length);
  recordCache(res);
  recordStatus(res, 'streaming');
}

export function handleSummary(data) {
  return summary(data, __ENV.BENCH_LABEL || 'streaming');
}
