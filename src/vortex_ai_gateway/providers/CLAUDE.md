# providers/ — the vendor seam

Loaded when work happens in this directory. The root `AGENTS.md` has the
repo-wide rules; this file has the ones that only matter here.

## The seam is two methods wide

`base.py` defines `ChatProvider` as a `runtime_checkable` `Protocol` with
`name`, `complete()` and `stream()`. It is **structural** — adapters owe it
nothing but those members, and `HttpChatAdapter` is shared plumbing, not the
seam. Widen the protocol (model listing, embeddings, health) only when something
above it actually needs the new method, not in anticipation (ADR-011).

Implementations **raise** on failure rather than returning an error envelope.
Mapping an exception to a status code is `routes.py`'s job, so a provider never
has to know what HTTP status its caller will use.

## What an adapter owes, and what it must not do

Set four class vars — `provider_name`, `default_base_url`, `chat_path`,
`requires_api_key` — and implement `_headers`, `_encode`, `_decode`, `stream`.

Do **not**:

- Override `_post`, `_lines`, `_transport_error` or `_status_error`. Every
  `httpx.HTTPError` and non-2xx status is classified once, in `http.py`, onto
  the typed hierarchy in `errors.py` with its `retryable` flag (ADR-015). The
  adapter's only error job is `self._protocol_error(...)` for a 200 whose body
  does not parse.
- Add any retry, sleep, backoff or breaker. `build_router` wraps every adapter
  in `ResilientProvider` before the router sees it, so retries here *multiply*
  with the wrapper's (ADR-001, ADR-002).
- Let anything vendor-shaped escape the module — no vendor dict, exception or
  enum crosses the seam (ADR-009).
- Silently drop a parameter the vendor cannot express. Raise
  `UnsupportedParameterError` from a `_reject_unsupported()` called by
  `_encode` (ADR-014). `anthropic.py` and `ollama.py` both have one.

## Reaching an adapter through the router

`router.providers` holds **wrappers**, not adapters. The adapter is `.inner`
(ADR-020). Code or a test that reaches for `router.providers[name]` expecting an
`OpenAIAdapter` gets a `ResilientProvider`.

## Model-name aliasing

`upstream_model()` and `metadata()` in the base own this: the caller gets their
alias back in `response.model`, and the vendor's real ID is preserved in
`response.vortex.upstream_model`. Pass `models={"alias": "real-id"}` at
construction — never rewrite names inside `_encode`.

Because a fallback chain forwards the request **unchanged**, a chain is only
valid between providers that answer to the same model names. Per-hop rewriting
is deliberately not built (ADR-020).

## Two local details

- `errors.py` has an `N818` waiver in `pyproject.toml` — the taxonomy is named
  for what happened, not for the fact that it is an exception, because
  `except ProviderRateLimited:` reads as a sentence. That waiver applies to
  that one module and nowhere else.
- An injected `httpx.AsyncClient` is never closed by `aclose()`; the adapter
  does not own it. Tests rely on this.

Adding a vendor touches eight files — use the `add-provider` skill rather than
working from memory.
