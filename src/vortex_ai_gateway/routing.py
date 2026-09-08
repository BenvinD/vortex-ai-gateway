"""Choosing which provider serves a model.

The gateway's whole promise is that a caller names a *model* and the right
vendor answers. That mapping is configuration, not code: it changes when a
model is retired, a key rotates or traffic shifts, and none of those should be
a code change (ADR-016).

The table is an **ordered** list of ``pattern=provider`` rules, first match
wins::

    VORTEX_MODEL_ROUTES=gpt-4o=openai,gpt-*=openai,claude-*=anthropic,local/*=ollama

Order is what makes it expressive: a specific rule placed before a general one
carves an exception out of it, which a plain dictionary could not express.
Patterns are shell globs (:func:`fnmatch.fnmatchcase`) — familiar, and small
enough that a rule can be read aloud. Regular expressions were rejected as more
power than routing needs and far more ways to be subtly wrong in a deployment
console.

:class:`ProviderRouter` is itself a
:class:`~vortex_ai_gateway.providers.base.ChatProvider`, so the router the app
mounts is indistinguishable from a single adapter — routing composes through
the same seam rather than sitting beside it.
"""

import fnmatch
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass

import structlog

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from vortex_ai_gateway.providers import (
    AnthropicAdapter,
    ChatProvider,
    HttpChatAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    ProviderBadRequest,
)
from vortex_ai_gateway.providers.resilience_wrapper import (
    FallbackProvider,
    wrap_with_resilience,
)

logger = structlog.get_logger(__name__)

#: Every provider name a routing rule may name, and the adapter behind it.
#: Adding a fourth vendor means adding a row here and a matching
#: ``<name>_api_key`` / ``<name>_base_url`` pair on
#: :class:`~vortex_ai_gateway.config.Settings`.
ADAPTERS: dict[str, type[HttpChatAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": OllamaAdapter,
}


class RoutingConfigError(ValueError):
    """The routing configuration cannot be turned into a working router.

    Raised at startup, deliberately: a gateway that boots with an unusable
    routing table only discovers it on the first real request, which is the
    worst possible moment to find out.
    """


class UnroutableModelError(ProviderBadRequest):
    """No rule claims this model.

    A :class:`~vortex_ai_gateway.providers.errors.ProviderBadRequest`, because
    that is what it is from the caller's side — a model name nothing serves —
    and because it must never be retried.
    """

    def __init__(self, model: str, *, patterns: Sequence[str]) -> None:
        known = ", ".join(patterns) or "none configured"
        super().__init__(
            f"No provider is configured for model {model!r}. Routing patterns: {known}.",
            provider="router",
            code="model_not_found",
        )
        self.model = model


def parse_fallback_chains(spec: str) -> dict[str, tuple[str, ...]]:
    """Parse ``"openai>anthropic,anthropic>ollama"`` into chains by head.

    Each chain is ordered and keyed on its first provider, which is the one a
    routing rule names. Whitespace is ignored so the value survives being
    wrapped across lines in a deployment manifest, as ``parse_routes`` does.
    """
    chains: dict[str, tuple[str, ...]] = {}
    for entry in spec.split(","):
        rule = entry.strip()
        if not rule:
            continue
        members = tuple(part.strip() for part in rule.split(">"))
        if len(members) < 2 or not all(members):
            raise RoutingConfigError(
                f"Fallback chain {rule!r} is not 'primary>next[>next]'; "
                "for example 'openai>anthropic'."
            )
        if len(set(members)) != len(members):
            raise RoutingConfigError(
                f"Fallback chain {rule!r} names a provider twice; a chain must not loop."
            )
        if members[0] in chains:
            raise RoutingConfigError(f"Provider {members[0]!r} heads more than one fallback chain.")
        chains[members[0]] = members
    return chains


@dataclass(frozen=True)
class ModelRoute:
    """One rule: models matching ``pattern`` go to ``provider``."""

    pattern: str
    provider: str

    def matches(self, model: str) -> bool:
        """Whether this rule claims ``model``.

        Case-sensitive on every platform: :func:`fnmatch.fnmatch` folds case
        according to the *host's* filesystem rules, which would make routing
        differ between a developer's machine and the container.
        """
        return fnmatch.fnmatchcase(model, self.pattern)

    def __str__(self) -> str:
        return f"{self.pattern}={self.provider}"


def parse_routes(spec: str) -> tuple[ModelRoute, ...]:
    """Parse ``"gpt-4o=openai,claude-*=anthropic"`` into an ordered table.

    Whitespace around entries is ignored so the value stays readable when it is
    wrapped across lines in a deployment manifest.
    """
    routes: list[ModelRoute] = []
    for entry in spec.split(","):
        rule = entry.strip()
        if not rule:
            continue
        pattern, separator, provider = (part.strip() for part in rule.partition("="))
        if not separator or not pattern or not provider:
            raise RoutingConfigError(
                f"Routing rule {rule!r} is not 'pattern=provider'; "
                "for example 'claude-*=anthropic'."
            )
        routes.append(ModelRoute(pattern=pattern, provider=provider))
    return tuple(routes)


class ProviderRouter:
    """A provider that delegates to another, chosen by the request's model.

    ``providers`` holds whatever was built for each name — in a configured
    deployment a :class:`~vortex_ai_gateway.providers.resilience_wrapper.ResilientProvider`
    or a :class:`~vortex_ai_gateway.providers.resilience_wrapper.FallbackProvider`
    around the adapter. One mapping, not two: a parallel "real" mapping kept
    only so tests can assert on adapter types is bookkeeping nobody enforces,
    and the wrappers expose ``.inner`` for anything that needs the adapter.
    """

    #: Reported in log lines. The provider that actually served a request names
    #: itself in ``response.vortex``, so this never masks the real one.
    name = "router"

    def __init__(
        self,
        routes: Sequence[ModelRoute],
        providers: Mapping[str, ChatProvider],
        default: str | None = None,
    ) -> None:
        wanted = {route.provider for route in routes} | ({default} if default else set())
        missing = sorted(wanted - set(providers))
        if missing:
            raise RoutingConfigError(
                f"Routing rules name providers that were not built: {', '.join(missing)}."
            )
        self.routes = tuple(routes)
        self.providers = dict(providers)
        self.default = default

    def __repr__(self) -> str:
        table = ", ".join(str(route) for route in self.routes)
        return f"ProviderRouter({table or 'no rules'}, default={self.default!r})"

    def provider_for(self, model: str) -> ChatProvider:
        """The provider that serves ``model``, or a refusal naming the table."""
        for route in self.routes:
            if route.matches(model):
                return self.providers[route.provider]
        if self.default is not None:
            return self.providers[self.default]
        raise UnroutableModelError(model, patterns=[str(route) for route in self.routes])

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        return await self.provider_for(request.model).complete(request)

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Delegate the stream, resolving the provider before it is iterated.

        Not an ``async def``: an unroutable model has to raise where the caller
        asks for the stream, not on the first chunk, or the failure arrives
        after the ``200`` has already gone out.
        """
        return self.provider_for(request.model).stream(request)

    async def aclose(self) -> None:
        """Close every provider that owns a connection pool.

        Wrappers delegate to what they hold, and a chain closes each member, so
        every adapter is reached exactly once through the mapping.
        """
        for provider in self.providers.values():
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()


def build_router(settings: Settings) -> ProviderRouter | None:
    """Build the router described by ``settings``, or ``None`` if unconfigured.

    Only the providers the table actually names are constructed — plus anything
    named as a fallback for one of them — so an unrelated missing key cannot
    stop the gateway booting, while a *named* provider missing its key stops it
    immediately rather than turning into a ``401`` on the first request that
    routes there.

    Every adapter is wrapped in its own retry loop and its own breaker before
    anything else sees it, so one vendor's outage cannot open another vendor's
    circuit (ADR-002). Where a fallback chain names it, the wrapped providers
    are then composed into a chain (ADR-020).
    """
    routes = parse_routes(settings.model_routes)
    default = settings.default_provider.strip() or None
    if not routes and default is None:
        return None

    chains = parse_fallback_chains(settings.fallback_chains)
    routed = {route.provider for route in routes} | ({default} if default else set())
    # A fallback member has to be built and key-checked like any other provider,
    # so it joins the enable list.
    wanted = set(routed)
    for head in routed & set(chains):
        wanted.update(chains[head])
    names = sorted(wanted)

    unknown = [name for name in names if name not in ADAPTERS]
    if unknown:
        raise RoutingConfigError(
            f"Unknown provider(s) in VORTEX_MODEL_ROUTES or VORTEX_FALLBACK_CHAINS: "
            f"{', '.join(unknown)}. Known providers: {', '.join(sorted(ADAPTERS))}."
        )
    unreachable = sorted(set(chains) - routed)
    if unreachable:
        raise RoutingConfigError(
            f"Fallback chain(s) head a provider no routing rule sends traffic to: "
            f"{', '.join(unreachable)}. Add a rule to VORTEX_MODEL_ROUTES or drop the chain."
        )

    resilient = {
        name: wrap_with_resilience(_build_adapter(name, settings), settings) for name in names
    }
    providers: dict[str, ChatProvider] = dict(resilient)
    for head, members in chains.items():
        providers[head] = FallbackProvider([resilient[member] for member in members], name=head)

    router = ProviderRouter(routes=routes, providers=providers, default=default)
    logger.info(
        "provider routing configured",
        routes=[str(route) for route in routes],
        default_provider=default,
        providers=names,
        fallback_chains=[" > ".join(members) for members in chains.values()],
        retry_max_attempts=settings.retry_max_attempts,
        breaker_failure_threshold=settings.breaker_failure_threshold,
    )
    return router


def _build_adapter(name: str, settings: Settings) -> HttpChatAdapter:
    """Construct one adapter from the ``<name>_*`` settings that describe it."""
    adapter_type = ADAPTERS[name]
    api_key = str(getattr(settings, f"{name}_api_key", "") or "")
    base_url = str(getattr(settings, f"{name}_base_url", "") or "")

    if adapter_type.requires_api_key and not api_key:
        raise RoutingConfigError(
            f"Provider {name!r} is routed to but has no API key; set VORTEX_{name.upper()}_API_KEY."
        )

    return adapter_type(
        api_key=api_key or None,
        base_url=base_url or None,
        timeout=settings.request_timeout_seconds,
        connect_timeout=settings.connect_timeout_seconds,
    )
