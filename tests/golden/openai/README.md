# Golden files — OpenAI wire format

Each file is one response exactly as OpenAI's API returns it. The tests in
`tests/test_golden_openai.py` parse every file in these directories with the
matching contract model and assert that **nothing in the file is dropped or
altered** by the round trip. They are the check that
`vortex_ai_gateway.contracts` still describes the real wire format rather than
our memory of it.

| Directory | Parsed as | Format |
|---|---|---|
| `completions/` | `ChatCompletionResponse` | one JSON object |
| `chunks/` | `ChatCompletionChunk` | JSON Lines — one streamed chunk per line |
| `errors/` | `ErrorResponse` | one JSON object |
| `models/` | `ModelList` | one JSON object |

## Provenance

These were written against OpenAI's documented response schema, not captured
from a live account — this repo has no key committed to it. They are therefore
a *reconstruction*, and the usual caveat applies: a reconstruction can only
encode what we already believe.

**Replacing them with real captures is the point.** The loader globs these
directories, so a genuine capture needs no code change:

```bash
curl https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}' \
  > tests/golden/openai/completions/<describe-the-case>.json

# streaming: strip the SSE framing, keep one JSON object per line
curl -N https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"stream":true}' \
  | sed -n 's/^data: //p' | grep -v '^\[DONE\]$' \
  > tests/golden/openai/chunks/<describe-the-case>.jsonl
```

A capture that fails to parse is not a broken test — it is the contract being
out of date, and the fix belongs in `contracts/`, not here. Scrub any real key,
organisation ID or user content before committing one.
