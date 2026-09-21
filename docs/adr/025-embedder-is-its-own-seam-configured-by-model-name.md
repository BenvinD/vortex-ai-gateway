# ADR-025: The embedder is its own seam outside `providers/`, switched on by naming a model
Date / Status: 2026-09-21 / accepted
Context: ADR-024 left the semantic tier behind an `Embedder` protocol with no
implementation, and ADR-005 left "which model embeds" to the deployment. Turning
the tier on therefore meant writing code: `create_app(embedder=...)`. The first
implementation has to decide where an embedder lives relative to the chat
adapters, how it fails, and how a deployment names one without a code change.
Options: For placement: A) a fourth adapter under `providers/`, on
`HttpChatAdapter`, so it inherits the client, timeouts and ADR-015's
status→exception taxonomy B) its own module, `embedding.py`, with the same
two-method shape (`embed`, `aclose`) and nothing shared but the timeout
constants C) widen `ChatProvider` with an `embed()` method so the Ollama
adapter serves both. For failure: one `EmbeddingError`, or the full
`ProviderError` taxonomy with `retryable` flags. For configuration: a
`VORTEX_EMBEDDING_MODEL` name with the URL inherited from `ollama_base_url`, or
a `provider:model` scheme, or a full URL per embedder.
Decision: B; one error; a model name. `providers/` is the `ChatProvider` seam
and it is two methods wide on purpose (ADR-011); an embedder is one call and
one vector, with no stream, no usage and no bill to reconcile, and sharing the
chat base class would make a 429 from an embedding server a `ProviderRateLimited`
that nothing upstream is prepared to retry — the semantic cache's whole response
to any embedder failure is one warning and an uncached answer (`DEGRADED_EVENT`),
so a taxonomy the caller collapses to one branch is a taxonomy nobody reads.
Option C was rejected because ADR-011 says widen the protocol only when something
above it needs the method, and the semantic cache does not need a *chat*
provider to embed. The switch is the model name alone because that is the one
decision ADR-005 leaves open and the one a threshold is tied to; there is no
default model, so naming one is a choice made on purpose, and the URL falls back
to the chat adapter's Ollama so one local server is configured once. `/api/embed`
rather than the older `/api/embeddings`, because that is what
`scripts/threshold_experiment.py` calls, and a vector must be comparable with the
ones the threshold was measured on. Not built: a hosted embedding API, and any
retry — the wrapper that retries chat calls is not in this path, and a cache
lookup that retries is a cache that adds latency on the miss it exists to avoid.
Consequences: The tier is now a three-variable procedure — enable, name a model,
point at a server — and the France checkpoint passes live at 0.966. So does the
false hit ADR-005 warned about: *5 km to miles* was served *5 miles to km* at
0.996 on the same server, which is why the tier stays off by default and this ADR
changes nothing in ADR-005. A second embedder backend is a second class in
`embedding.py` with the same two methods, and the model-name switch grows a
prefix the day two backends can serve the same name. Placement is revisited if
the chat and embedding calls ever need one shared budget or breaker — then the
embedder moves behind the resilience wrapper, not into `providers/`.
