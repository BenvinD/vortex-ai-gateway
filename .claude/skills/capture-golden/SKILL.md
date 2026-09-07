---
name: capture-golden
description: Capture a real vendor response into tests/golden/ so the contract models are checked against actual wire bytes instead of a reconstruction. Use when asked to "capture a golden file", "record a real response", "refresh the fixtures", or when a contract change needs evidence from the live API.
---

# capture-golden — replace a reconstruction with real bytes

The files under `tests/golden/openai/` were written against OpenAI's documented
schema, **not captured from a live account** — this repo has no key committed.
They are a reconstruction, and per their own README the usual caveat applies: *a
reconstruction can only encode what we already believe.* Replacing them with
genuine captures is the point of the directory.

**Requires a real vendor key in the environment.** If none is set, stop and say
so — do not synthesise a file and file it as a capture. A hand-written fixture is
what is already there.

## The loader needs no code change

`tests/test_golden_openai.py` globs these directories, so a new file is picked up
by dropping it in. One case per file; name the file after the case, not the
model.

| Directory | Parsed as | Format |
|---|---|---|
| `completions/` | `ChatCompletionResponse` | one JSON object |
| `chunks/` | `ChatCompletionChunk` | JSON Lines — one streamed chunk per line |
| `errors/` | `ErrorResponse` | one JSON object |
| `models/` | `ModelList` | one JSON object |

## Buffered

```bash
curl https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}' \
  > tests/golden/openai/completions/<describe-the-case>.json
```

## Streamed — strip the SSE framing, keep one object per line

```bash
curl -N https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"stream":true}' \
  | sed -n 's/^data: //p' | grep -v '^\[DONE\]$' \
  > tests/golden/openai/chunks/<describe-the-case>.jsonl
```

## Scrub before committing — every time

Remove or replace any real API key, organisation ID, project ID, and any user
content that should not be in a public repo. Then confirm nothing leaked:

```bash
grep -rEi 'sk-[A-Za-z0-9]|org-[A-Za-z0-9]|proj_[A-Za-z0-9]' tests/golden/ && echo "LEAK — fix before committing"
```

Also drop or fuzz `id` and `created` only if they carry account information;
otherwise leave the response byte-identical. The value of a capture is that it
is unedited.

## When it fails to parse

> A capture that fails to parse is not a broken test — it is the contract being
> out of date, and the fix belongs in `contracts/`, not here.

So: do **not** edit the captured file to make the test pass. Fix the model in
`contracts/`, and if the wire format changed in a way that affects what the
gateway promises, that is an ADR (`/adr`).

## Other vendors

The same shape works for any vendor — `tests/golden/<vendor>/` — but the loader
in `test_golden_openai.py` is OpenAI-specific. A second vendor needs its own
parametrised loader test alongside it.
