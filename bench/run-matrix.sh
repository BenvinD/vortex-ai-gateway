#!/usr/bin/env bash
#
# The benchmark matrix: four workloads across the two configuration axes that
# change the answer.
#
#   workers    1 vs 4      — breaker state, cache counters, Prometheus counters
#                            and the semantic index are all per worker process,
#                            so "does this scale across processes" is not a
#                            question the code answers by inspection.
#   semantic   off vs on   — the tier costs an embedding call on every exact-tier
#                            miss. ADR-005 keeps it off; what it costs is the
#                            other half of that decision.
#
# Everything else is held fixed, and deliberately: a sweep of every switch is
# 64 arms nobody reads. See docs/design/day-13.md.
#
#   bench/run-matrix.sh                       # the full matrix
#   BENCH_WORKERS=1 bench/run-matrix.sh       # one worker only
#   BENCH_SEMANTIC_ARMS=off bench/run-matrix.sh
#   BENCH_WORKLOADS="baseline cache-hit" bench/run-matrix.sh
#   BENCH_MODE=rate BENCH_RATE=2000 bench/run-matrix.sh
#
# Results land in bench/results/<timestamp>/ — one JSON summary per arm, a
# /metrics scrape either side of each run, and a manifest recording what the
# numbers were measured on. Nothing here writes to the repository's own Redis,
# its own port, or its own .env: the gateway under test is started by this
# script with an explicit environment, because a benchmark that inherits
# configuration measures something nobody can reproduce.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"

# --- knobs -----------------------------------------------------------------

BENCH_WORKERS="${BENCH_WORKERS:-1 4}"
BENCH_SEMANTIC_ARMS="${BENCH_SEMANTIC_ARMS:-off on}"
BENCH_WORKLOADS="${BENCH_WORKLOADS:-baseline cache-hit streaming provider-failure}"
BENCH_PORT="${BENCH_PORT:-8100}"
BENCH_REDIS_PORT="${BENCH_REDIS_PORT:-6399}"
BENCH_DURATION="${BENCH_DURATION:-30s}"
BENCH_VUS="${BENCH_VUS:-32}"
BENCH_MODE="${BENCH_MODE:-vus}"
BENCH_RATE="${BENCH_RATE:-1000}"
BENCH_EMBEDDING_MODEL="${BENCH_EMBEDDING_MODEL:-nomic-embed-text}"
BENCH_OLLAMA_URL="${BENCH_OLLAMA_URL:-http://127.0.0.1:11434}"

# Packets to this address are blackholed, so each upstream attempt costs a full
# connect timeout. That is what makes the breaker's refusal worth measuring —
# a refused connection fails in microseconds and makes the breaker look
# unnecessary. See the header of bench/provider-failure.js.
BENCH_DEAD_UPSTREAM="${BENCH_DEAD_UPSTREAM:-http://10.255.255.1:1}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$REPO/bench/results/$STAMP"
RUN="$(mktemp -d)"

# --- prerequisites ---------------------------------------------------------

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "bench: '$1' is not installed. $2" >&2
    exit 1
  }
}

need k6 "brew install k6 (or see https://grafana.com/docs/k6/latest/set-up/install-k6/)"
need uv "https://docs.astral.sh/uv/"
need redis-server "brew install redis"
need redis-cli "brew install redis"
need curl ""

# The gateway must be importable from the *installed* distribution, which is the
# same rule the test suite follows (AGENTS.md, "The src/ layout is load-bearing").
# A benchmark of a stale installed copy is a benchmark of last week's code.
uv sync --quiet

# --- teardown --------------------------------------------------------------

GATEWAY_PID=""
REDIS_PID=""

cleanup() {
  local code=$?
  stop_gateway
  if [[ -n "$REDIS_PID" ]] && kill -0 "$REDIS_PID" 2>/dev/null; then
    kill "$REDIS_PID" 2>/dev/null || true
    wait "$REDIS_PID" 2>/dev/null || true
  fi
  rm -rf "$RUN"
  exit $code
}
trap cleanup EXIT INT TERM

# --- redis -----------------------------------------------------------------

if redis-cli -p "$BENCH_REDIS_PORT" ping >/dev/null 2>&1; then
  echo "bench: something is already listening on redis port $BENCH_REDIS_PORT." >&2
  echo "       Set BENCH_REDIS_PORT to a free port; this script will not share" >&2
  echo "       a Redis with anything, because it flushes between arms." >&2
  exit 1
fi

# `--save ""` for the same reason compose.yaml sets it: everything in here is a
# cache entry or a rate-limit bucket, and a fork-to-disk pause is a latency
# spike in the numbers we came here to measure.
redis-server --port "$BENCH_REDIS_PORT" --save "" --appendonly no \
  --dir "$RUN" --logfile "$RUN/redis.log" --daemonize no &
REDIS_PID=$!

for _ in $(seq 1 50); do
  redis-cli -p "$BENCH_REDIS_PORT" ping >/dev/null 2>&1 && break
  sleep 0.1
done
redis-cli -p "$BENCH_REDIS_PORT" ping >/dev/null 2>&1 || {
  echo "bench: redis did not come up on $BENCH_REDIS_PORT; see $RUN/redis.log" >&2
  exit 1
}

# --- the gateway under test ------------------------------------------------

start_gateway() {
  local workers="$1" semantic="$2" broken="$3" label="$4"

  # **Every** setting is named, not just the ones this arm varies. Two reasons,
  # and both of them are "a benchmark nobody can reproduce is not a benchmark":
  #
  #   `Settings` reads a `.env` in the repository root as a lower-priority
  #   source, and a real environment variable beats it — so a knob this script
  #   does not set is one a developer's local `.env` silently chooses. Naming
  #   all of them is what makes the arm the same arm on every machine.
  #
  #   The rate limits are enormous rather than zero. A principal with no limits
  #   never reaches Redis at all (`RateLimiter.admit` returns early), so zero
  #   would quietly lift the limiter's Lua round trip *out* of the measurement,
  #   and the deployment being benchmarked is one that runs the limiter.
  #
  # VORTEX_API_KEYS stays empty, so any well-formed bearer token is accepted and
  # the whole run shares one key_id — which it must, because the cache, the
  # buckets and the ledger are all namespaced by it.
  local semantic_on=false embedding_model="" routes="" openai_key="" openai_url=""
  [[ $semantic == on ]] && semantic_on=true && embedding_model="$BENCH_EMBEDDING_MODEL"
  if [[ $broken == yes ]]; then
    routes='broken-*=openai'
    openai_key='sk-bench-not-a-real-key'
    openai_url="$BENCH_DEAD_UPSTREAM"
  fi

  env \
    VORTEX_ENVIRONMENT=local \
    VORTEX_LOG_LEVEL="${BENCH_LOG_LEVEL:-INFO}" \
    VORTEX_REDIS_URL="redis://127.0.0.1:$BENCH_REDIS_PORT/0" \
    VORTEX_REQUEST_TIMEOUT_SECONDS=30.0 \
    VORTEX_CONNECT_TIMEOUT_SECONDS=5.0 \
    VORTEX_API_KEYS="" \
    VORTEX_KEY_DB_PATH="" \
    VORTEX_MODEL_ROUTES="$routes" \
    VORTEX_DEFAULT_PROVIDER="" \
    VORTEX_OPENAI_API_KEY="$openai_key" \
    VORTEX_OPENAI_BASE_URL="$openai_url" \
    VORTEX_ANTHROPIC_API_KEY="" \
    VORTEX_ANTHROPIC_BASE_URL="" \
    VORTEX_OLLAMA_API_KEY="" \
    VORTEX_OLLAMA_BASE_URL="$BENCH_OLLAMA_URL" \
    VORTEX_RETRY_MAX_ATTEMPTS=3 \
    VORTEX_RETRY_BACKOFF_SECONDS=0.5 \
    VORTEX_RETRY_MAX_BACKOFF_SECONDS=10.0 \
    VORTEX_RETRY_DEADLINE_SECONDS=90.0 \
    VORTEX_RETRY_BUDGET_CAPACITY=0 \
    VORTEX_RETRY_BUDGET_REFILL_PER_SECOND=0.0 \
    VORTEX_BREAKER_FAILURE_THRESHOLD="${BENCH_BREAKER_THRESHOLD:-5}" \
    VORTEX_BREAKER_RESET_SECONDS=60.0 \
    VORTEX_BREAKER_BACKOFF_MULTIPLIER=2.0 \
    VORTEX_BREAKER_MAX_OPEN_SECONDS=600.0 \
    VORTEX_FALLBACK_CHAINS="" \
    VORTEX_METERING_ENABLED=true \
    VORTEX_RATE_LIMIT_DEFAULT_RPM=100000000 \
    VORTEX_RATE_LIMIT_DEFAULT_TPM=100000000000 \
    VORTEX_RATE_LIMIT_ASSUMED_COMPLETION_TOKENS=512 \
    VORTEX_PRICE_TABLE_PATH="" \
    VORTEX_USAGE_RETENTION_DAYS=30 \
    VORTEX_CACHE_ENABLED=true \
    VORTEX_CACHE_TTL_SECONDS=600 \
    VORTEX_CACHE_TTLS="" \
    VORTEX_CACHE_SCOPE=key \
    VORTEX_SEMANTIC_CACHE_ENABLED="$semantic_on" \
    VORTEX_SEMANTIC_CACHE_THRESHOLD="${BENCH_SEMANTIC_THRESHOLD:-0.95}" \
    VORTEX_EMBEDDING_MODEL="$embedding_model" \
    VORTEX_EMBEDDING_BASE_URL="" \
    VORTEX_SERVICE_NAME=vortex-bench \
    VORTEX_TRACING_ENABLED=false \
    VORTEX_TRACING_EXPORTER=otlp \
    VORTEX_TRACING_ENDPOINT="" \
    VORTEX_TRACING_SAMPLE_RATIO=1.0 \
    VORTEX_METRICS_ENABLED=true \
    VORTEX_METRICS_PATH=/metrics \
    VORTEX_METRICS_LABEL_BUDGET=50 \
    uv run --no-sync uvicorn vortex_ai_gateway.gateway:app \
      --host 127.0.0.1 --port "$BENCH_PORT" --workers "$workers" \
      --log-level warning --no-access-log \
      >"$OUT/$label.gateway.log" 2>&1 &

  GATEWAY_PID=$!

  for _ in $(seq 1 100); do
    curl -fsS "http://127.0.0.1:$BENCH_PORT/healthz" >/dev/null 2>&1 && return 0
    kill -0 "$GATEWAY_PID" 2>/dev/null || break
    sleep 0.2
  done

  echo "bench: gateway did not become healthy; tail of $OUT/$label.gateway.log:" >&2
  tail -20 "$OUT/$label.gateway.log" >&2 || true
  return 1
}

stop_gateway() {
  [[ -z "$GATEWAY_PID" ]] && return 0
  if kill -0 "$GATEWAY_PID" 2>/dev/null; then
    # TERM, then wait: uvicorn's shutdown is what closes the Redis pool and
    # flushes whatever the worker was holding.
    kill "$GATEWAY_PID" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$GATEWAY_PID" 2>/dev/null || break
      sleep 0.1
    done
    kill -9 "$GATEWAY_PID" 2>/dev/null || true
  fi
  wait "$GATEWAY_PID" 2>/dev/null || true
  GATEWAY_PID=""
}

scrape() {
  curl -fsS "http://127.0.0.1:$BENCH_PORT/metrics" -o "$1" 2>/dev/null || true
}

# --- the matrix ------------------------------------------------------------

mkdir -p "$OUT"

semantic_available() {
  curl -fsS -m 3 "$BENCH_OLLAMA_URL/api/tags" 2>/dev/null \
    | grep -q "$BENCH_EMBEDDING_MODEL"
}

{
  echo "when          $STAMP"
  echo "host          $(uname -srm)"
  echo "cpus          $(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu)"
  echo "python        $(uv run --no-sync python -V 2>&1)"
  echo "k6            $(k6 version 2>&1 | head -1)"
  echo "redis         $(redis-server --version | head -1)"
  echo "gateway       $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo ".env present  $([[ -f "$REPO/.env" ]] && echo "yes — overridden field by field, see start_gateway" || echo no)"
  echo "dirty         $(git -C "$REPO" status --porcelain 2>/dev/null | wc -l | tr -d ' ') uncommitted files"
  echo "mode          $BENCH_MODE ($([[ $BENCH_MODE == rate ]] && echo "$BENCH_RATE/s" || echo "$BENCH_VUS VUs"))"
  echo "duration      $BENCH_DURATION"
  echo "workers       $BENCH_WORKERS"
  echo "semantic      $BENCH_SEMANTIC_ARMS"
  echo "workloads     $BENCH_WORKLOADS"
  echo "dead upstream $BENCH_DEAD_UPSTREAM"
} | tee "$OUT/manifest.txt"
echo

for semantic in $BENCH_SEMANTIC_ARMS; do
  if [[ $semantic == on ]] && ! semantic_available; then
    echo "bench: skipping the semantic arm — no '$BENCH_EMBEDDING_MODEL' at $BENCH_OLLAMA_URL."
    echo "       ollama serve && ollama pull $BENCH_EMBEDDING_MODEL"
    echo "skipped: no embedder" > "$OUT/semantic-on.skipped"
    continue
  fi

  for workers in $BENCH_WORKERS; do
    for workload in $BENCH_WORKLOADS; do
      label="$workload-w$workers-sem$semantic"
      broken=no
      [[ $workload == provider-failure ]] && broken=yes

      echo "── $label"

      # Flushed between arms, always. A cache warmed by the previous arm is a
      # hit rate that belongs to a run nobody is reading.
      redis-cli -p "$BENCH_REDIS_PORT" flushall >/dev/null

      start_gateway "$workers" "$semantic" "$broken" "$label" || exit 1
      scrape "$OUT/$label.metrics.before.txt"

      BENCH_URL="http://127.0.0.1:$BENCH_PORT" \
      BENCH_LABEL="$label" \
      BENCH_OUT="$OUT" \
      BENCH_MODE="$BENCH_MODE" \
      BENCH_VUS="$BENCH_VUS" \
      BENCH_RATE="$BENCH_RATE" \
      BENCH_DURATION="$BENCH_DURATION" \
      BENCH_SEMANTIC="$semantic" \
        k6 run --quiet "bench/$workload.js" 2>&1 | tee "$OUT/$label.k6.txt" || {
          echo "bench: $label failed a threshold; the result is kept." >&2
        }

      scrape "$OUT/$label.metrics.after.txt"
      stop_gateway
      echo
    done
  done
done

echo "results  $OUT"
echo
echo "Next:  uv run bench/report.py $OUT"
