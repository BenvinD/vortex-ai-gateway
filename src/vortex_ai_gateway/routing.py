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
    """A provider that delegates to another, chosen by the request's model."""

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
        """Close every provider that owns a connection pool."""
        for provider in self.providers.values():
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()


def build_router(settings: Settings) -> ProviderRouter | None:
    """Build the router described by ``settings``, or ``None`` if unconfigured.

    Only the providers the table actually names are constructed, so an
    unrelated missing key cannot stop the gateway booting — and a *named*
    provider missing its key stops it immediately, rather than turning into a
    ``401`` on the first request that routes there.
    """
    routes = parse_routes(settings.model_routes)
    default = settings.default_provider.strip() or None
    if not routes and default is None:
        return None

    names = sorted({route.provider for route in routes} | ({default} if default else set()))
    unknown = [name for name in names if name not in ADAPTERS]
    if unknown:
        raise RoutingConfigError(
            f"Unknown provider(s) in VORTEX_MODEL_ROUTES: {', '.join(unknown)}. "
            f"Known providers: {', '.join(sorted(ADAPTERS))}."
        )

    providers = {name: _build_adapter(name, settings) for name in names}
    router = ProviderRouter(routes=routes, providers=providers, default=default)
    logger.info(
        "provider routing configured",
        routes=[str(route) for route in routes],
        default_provider=default,
        providers=names,
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
