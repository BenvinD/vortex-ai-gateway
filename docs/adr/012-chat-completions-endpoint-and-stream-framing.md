# ADR-012: `/v1/chat/completions`, its stream framing, and the default provider
Date / Status: 2026-09-01 / accepted
Context: The contract and the provider seam are both testable in isolation, but
neither is reachable over HTTP. Exposing them raises three questions at once:
how a stream is framed, how an upstream failure is reported once a `200` is
already on the wire, and what serves the route before any vendor adapter exists.
Options: A) buffer every response and add streaming later B) SSE framed as
OpenAI does it — `data: {chunk}` per event, terminated by `data: [DONE]` —
with the provider injected into the router C) require a configured provider,
leaving the route 501 until an adapter lands.
Decision: B. `create_chat_router(provider)` takes its provider as an argument,
so tests mount the real routes over a scripted double with no patching. A
buffered failure is a `502` carrying the error envelope; a streaming failure
cannot be, so it is emitted as one final error event before `[DONE]`.
`create_app(provider=...)` defaults to `MockProvider`, which makes the endpoint
exercisable with no key, and logs a warning at startup because a deployment
answering from canned replies is the worst failure this service could have.
Consequences: The mock default must be replaced by explicit configuration
before anything real is deployed — a `501` (C) would be safer and is worth
revisiting once the first adapter lands.
Update (2026-09-08): the adapters landed (ADR-014) and the mock default stayed,
on the strength of one thing the `501` cannot do: `uv sync && uvicorn ...` with
no key and no config still serves a real request end to end, which is what makes
the seam demonstrable. The startup warning is the whole mitigation, and it is a
thin one — the safer answer is a boot-time refusal when `environment` is `prod`
and no routing table is configured, which is a better rule than `501` because it
distinguishes "unconfigured laptop" from "unconfigured deployment". Response bodies are dumped with
`exclude_none`, so absent fields are omitted rather than sent as `null`.
