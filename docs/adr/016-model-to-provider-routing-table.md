# ADR-016: An ordered glob table maps models to providers, first match wins
Date / Status: 2026-09-02 / accepted
Context: The gateway's promise is that a caller names a *model* and the right
vendor answers. That mapping changes when a model is retired, a key rotates or
traffic shifts to a cheaper deployment — none of which should be a code change,
and all of which have to be reviewable before they ship.
Options: A) a dict of exact model names B) an ordered list of `pattern=provider`
glob rules, first match wins C) regular expressions D) infer the provider from
the model name's prefix in code.
Decision: B, as one ordered value:
`VORTEX_MODEL_ROUTES=gpt-4o=openai,claude-*=anthropic,local/*=ollama`. Order is
what makes it expressive — a specific rule placed before a general one carves an
exception out of it, which A cannot express and C only expresses obscurely.
`ProviderRouter` is itself a `ChatProvider`, so routing composes through the
existing seam instead of sitting beside it, and the app mounts it exactly as it
mounted a single adapter. The table is also the enable list: only the providers
it names are constructed, a named provider missing its key fails at *startup*,
and a model no rule claims is a `404`, not a silent fallback onto whichever
provider happened to be listed first.
Consequences: Two rules can overlap and the reader has to know the order
decides — the cost of the expressiveness. A default provider is available for
deployments that would rather forward an unknown model than reject it. Weighted
splits, fallbacks and health-aware selection do not fit this table; they belong
to load balancing (ADR-004 and the Redis work), which will consume this mapping
rather than replace it.
