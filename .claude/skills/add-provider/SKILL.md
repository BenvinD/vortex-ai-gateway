---
name: add-provider
description: Add a new vendor adapter behind the ChatProvider seam — the eight files a provider touches, the translation rules from ADR-014/015, and the test shape it needs. Use when adding or wiring up any model vendor (Bedrock, Vertex, Groq, Mistral, Together, a self-hosted runtime), or when asked to "add a provider", "support <vendor>", or "write an adapter".
---

# add-provider — the eight places a vendor touches

The adapters are the most template-heavy work in this repo: 480–600 lines of
adapter plus 450–690 lines of tests each. Almost all of that shape is already
decided. Read `providers/openai.py` first if the vendor speaks OpenAI's format,
`providers/anthropic.py` if it has its own message shape, and
`providers/ollama.py` if it is a local runtime with no key.

Substitute `<vendor>` (lowercase, the routing-table name) throughout.

## The checklist

Every one of these is required. Miss 2 and routing cannot find the adapter;
miss 3 and the gateway crashes at boot; miss 6 and the model bills to nobody.

1. **`src/vortex_ai_gateway/providers/<vendor>.py`** — subclass
   `HttpChatAdapter`. Set the four class vars and implement the translation
   pair:

   ```python
   class VendorAdapter(HttpChatAdapter):
       provider_name: ClassVar[str] = "<vendor>"
       default_base_url: ClassVar[str] = "https://api.vendor.com"
       chat_path: ClassVar[str] = "/v1/chat/completions"
       requires_api_key: ClassVar[bool] = True   # False only for a local runtime

       def _headers(self) -> dict[str, str]: ...

       def _encode(
           self, request: ChatCompletionRequest, model: str, *, stream: bool
       ) -> dict[str, Any]: ...

       def _decode(
           self, payload: Any, request: ChatCompletionRequest, model: str
       ) -> ChatCompletionResponse: ...

       async def stream(
           self, request: ChatCompletionRequest
       ) -> AsyncIterator[ChatCompletionChunk]: ...
   ```

   Copy those signatures exactly — they are the base's translation hooks, and
   `model` is the *upstream* name `upstream_model()` already resolved, not the
   caller's alias. `_encode` takes `stream` keyword-only because one encoder
   serves both paths; `stream()` is declared on the base but unimplemented, so a
   half-finished adapter is a type error rather than a runtime surprise.

   Do **not** override `_post`, `_lines`, `_transport_error` or `_status_error`.
   Failure classification happens once, in the base — see rule 3 below.

2. **`src/vortex_ai_gateway/providers/__init__.py`** — import the class, and add
   it to `__all__`. The list is alphabetised; ruff's `RUF022` will tell you if
   it is not.

3. **`src/vortex_ai_gateway/routing.py`** — add one entry to `ADAPTERS`:

   ```python
   ADAPTERS: dict[str, type[HttpChatAdapter]] = {..., "<vendor>": VendorAdapter}
   ```

   This dict key is the name used in `VORTEX_MODEL_ROUTES` and the stem
   `_build_adapter` uses for `getattr(settings, f"{name}_api_key")`. It must
   match the config field prefix in step 4 exactly, or the adapter is built with
   no credential.

4. **`src/vortex_ai_gateway/config.py`** — add the pair, next to the others:

   ```python
   <vendor>_api_key: str = ""
   <vendor>_base_url: str = ""
   ```

   Nothing else. `_build_adapter` finds them by name; there is no registry to
   update. A provider named in the routing table without its key stops the
   gateway at startup rather than on the first request.

5. **`.env.example`** — add `VORTEX_<VENDOR>_API_KEY=` and
   `VORTEX_<VENDOR>_BASE_URL=` under "Provider routing", with a comment saying
   what an empty base URL means for this vendor.

6. **`src/vortex_ai_gateway/pricing.py`** — add rows to `DEFAULT_PRICES`, USD
   per million tokens, **ordered most specific first** (the first matching glob
   wins). A model with no row returns `None`, not zero — so skipping this does
   not make the model free, it makes it unbilled and reported as unpriced. If
   the vendor runs on your own hardware, `Decimal("0")` is a real price and the
   right one.

7. **`tests/test_<vendor>_adapter.py`** — see the test shape below.

8. **`README.md`** — the provider-routing section lists the vendors and their
   env vars. Add the row.

Then: `/verify`, and `/adr` if any translation decision was non-obvious.

## The three translation rules that are not negotiable

**Refuse what the vendor cannot express; never drop it silently (ADR-014).**
Give the adapter a `_reject_unsupported(request)` that raises
`UnsupportedParameterError` for every contract field the vendor has no
equivalent for, and call it from `_encode`. Both `anthropic.py` and `ollama.py`
have one — copy the shape. A parameter accepted and ignored is a caller who
thinks `logprobs` worked.

**Nothing vendor-shaped crosses the seam (ADR-009, ADR-011).** The adapter takes
a `ChatCompletionRequest` and returns a `ChatCompletionResponse`. No vendor dict,
no vendor exception, no vendor enum escapes the module. The gateway above speaks
OpenAI's vocabulary and nothing else.

**Classify failures in the base, not the adapter (ADR-015).** `_post` and
`_lines` already map every `httpx.HTTPError` and every non-2xx status onto the
typed hierarchy in `providers/errors.py`, each carrying its `retryable` flag.
Your adapter's only error job is `self._protocol_error(...)` when the vendor
returns 200 with a body that does not parse. If the vendor signals rate limits
in a non-standard way, extend `_status_error` in `http.py` — one place — rather
than catching in the adapter.

## Model-name translation

`upstream_model()` and `metadata()` in the base handle aliasing: the caller gets
their alias back in `response.model`, and the vendor's real ID is preserved in
`response.vortex.upstream_model`. Pass `models={"alias": "vendor-real-id"}` at
construction; do not rewrite names inside `_encode`.

Note the ADR-020 constraint this interacts with: a fallback chain forwards the
request **unchanged**, so a chain is only valid between providers answering to
the same model names. Per-hop rewriting is deliberately not built.

## Resilience — already handled, do not add it here

`build_router` wraps every adapter in `ResilientProvider` (retries + its own
breaker) before the router sees it, so `router.providers` holds wrappers and the
adapter is reached through `.inner` (ADR-001, ADR-002, ADR-020). Put **no**
retry loop, backoff or breaker in an adapter. A retry inside the adapter and a
retry in the wrapper multiply.

## The test shape

Model it on `tests/test_openai_adapter.py` (the short one) or
`tests/test_anthropic_adapter.py` (the thorough one, 689 lines). `respx` is the
dev dependency for mocking httpx; `asyncio_mode = "auto"` is set, so async tests
need no marker. Cover, at minimum:

- `_encode` for each message role, multi-part user content, tools, and
  `tool_choice`.
- `_decode` for text, tool calls, and each `finish_reason` the vendor emits.
- Streaming: deltas in order, `finish_reason` on the final choice, and the usage
  chunk. Usage is collected on **every** stream regardless of what the caller
  asked for, and stripped back out when they did not (ADR-018) — so the adapter
  must always request it upstream.
- Each `UnsupportedParameterError` the adapter raises.
- One malformed-200 body reaching `_protocol_error`.
- Each error status mapping onto the right typed exception with the right
  `retryable`.

Add golden files under `tests/golden/<vendor>/` only for a vendor whose real wire
bytes you have; the OpenAI ones are a documented reconstruction, and
`tests/golden/openai/README.md` explains why that caveat matters.
