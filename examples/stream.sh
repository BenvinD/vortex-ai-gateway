#!/usr/bin/env bash
# Watch tokens arrive live — and watch what happens when you walk away.
#
#   examples/stream.sh                       stream a reply, printing tokens as they land
#   examples/stream.sh "write a haiku"       stream your own prompt
#   examples/stream.sh --abandon 2           hang up after 2s, the way Ctrl-C would
#
# Start the gateway first:
#
#   uv run uvicorn vortex_ai_gateway.gateway:app | tee gateway.log
#
# With no routing table configured the gateway answers from its mock provider,
# so this works with no API key and no bill. Point VORTEX_MODEL_ROUTES at a real
# vendor to watch real tokens.
#
# Environment: GATEWAY (default http://127.0.0.1:8000), MODEL, VORTEX_API_KEY.
set -euo pipefail

GATEWAY="${GATEWAY:-http://127.0.0.1:8000}"
MODEL="${MODEL:-gpt-4o-mini}"
KEY="${VORTEX_API_KEY:-demo-key}"

ABANDON=""
if [[ "${1:-}" == "--abandon" ]]; then
    ABANDON="${2:-2}"
    shift $(($# > 1 ? 2 : 1))
fi
PROMPT="${1:-Count slowly from one to twenty, one number per line.}"

# Built with python rather than string-pasted, so a prompt containing quotes is
# a prompt rather than a syntax error.
BODY=$(python3 -c '
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": sys.argv[2]}],
    "stream": True,
    "stream_options": {"include_usage": True},
}))' "$MODEL" "$PROMPT")

RENDER=$(cat <<'PY'
import json, sys

for line in sys.stdin:
    if not line.startswith("data: "):
        # Not SSE framing at all. A rejected request answers with a plain JSON
        # envelope — a 404 for an unrouted model, a 401 for a bad key — and
        # dropping those lines is how a broken demo looks like a silent one.
        if line.strip():
            sys.stderr.write(line)
        continue
    payload = line.removeprefix("data: ").strip()
    if payload == "[DONE]":
        sys.stdout.write("\n--- [DONE] ---\n")
        break
    chunk = json.loads(payload)
    # A failure part-way through arrives as an error envelope in the stream:
    # the 200 went out long ago, so there is no status code left to use.
    if "error" in chunk:
        sys.stdout.write("\n--- stream failed: %s ---\n" % chunk["error"]["message"])
        continue
    for choice in chunk.get("choices", []):
        sys.stdout.write(choice["delta"].get("content") or "")
    if chunk.get("usage"):
        sys.stdout.write("\n--- usage: %s ---\n" % json.dumps(chunk["usage"]))
PY
)

# `-N` is the whole point: without it curl buffers, and the tokens arrive in one
# lump at the end, which looks identical to a non-streaming request. `-u` on the
# renderer is the same fix one process later.
stream() {
    curl -sN -X POST "$GATEWAY/v1/chat/completions" \
        -H "Authorization: Bearer $KEY" \
        -H 'content-type: application/json' \
        -d "$BODY" | python3 -u -c "$RENDER"
}

if [[ -z "$ABANDON" ]]; then
    stream
    exit 0
fi

echo "--- streaming for ${ABANDON}s, then hanging up ---"
stream &
CLIENT=$!
sleep "$ABANDON"
# SIGKILL rather than SIGINT: a background job in a non-interactive shell has
# SIGINT ignored, so the polite signal would be dropped and nothing would be
# demonstrated. From the gateway's side this is the same event as a Ctrl-C.
kill -KILL $CLIENT 2>/dev/null || true
wait $CLIENT 2>/dev/null || true

cat <<'MSG'

--- client is gone ---
The gateway should now have logged one record for this request:

    {"event": "stream finished", "outcome": "abandoned", "chunks": ..., "total_tokens": ...}

That line is the point. The upstream connection is closed when the client
leaves, so generation stops there instead of running to completion — and the
tokens that *were* generated are still accounted for, because somebody paid
for them. Check it with:

    grep 'stream finished' gateway.log | tail -1
MSG
