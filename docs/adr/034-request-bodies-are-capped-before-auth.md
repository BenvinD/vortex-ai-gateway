# ADR-034: Request bodies are capped in bytes, with a `413`, before the key is checked
Date / Status: 2026-10-24 / accepted
Context: FastAPI reads and holds the whole body before any dependency runs, so
before `require_api_key` (ADR-013), and nothing limited its size: anyone who
could reach the port chose how much memory a worker held. ADR-031's
`RequestTooLargeError` is a different limit — estimated tokens against a key's
TPM, decided after auth and after the body is parsed — and returns a `400`.
Options: A) a raw-ASGI middleware that refuses a declared `Content-Length`
unread and counts a chunked body as it arrives B) a `Content-Length` check
alone C) leave it to the reverse proxy in front D) check inside the route.
Decision: A, at 10 MiB by default (`VORTEX_MAX_REQUEST_BODY_BYTES`), mounted
innermost so the `413` carries the request ID and is logged and counted. A
128k-token prompt is about half a megabyte, so the default leaves room for a
few inline base64 images. B is bypassed by any client that sends chunked. C is
the right second layer but this gateway is meant to be deployable on its own,
and `compose.yaml` has no proxy. D runs after FastAPI has already buffered
the body, which is the cost being bounded. The middleware sends the `413`
itself rather than raising from `receive`, because FastAPI's body parser turns
any exception it does not recognise into a generic `400`.
Consequences: A body that fits is held by the middleware and replayed, which
costs nothing FastAPI was not already paying. An oversized body is refused
before the key is checked, so a caller with no key learns the limit, which is
not a secret. Revisit the default if image-heavy callers start hitting it, or
make it per key if one tenant legitimately needs far more than the rest.
