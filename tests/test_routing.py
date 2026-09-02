"""Tests for the model → provider routing table.

The table is configuration, so most of these are about what an operator can
type into it and what happens when they typo — including the cases that must
fail at *startup* rather than on the first request that hits them.
"""

from typing import Any

import httpx
import pytest
import respx

from tests.upstream import chat_request
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import (
    AnthropicAdapter,
    CannedReply,
    ChatProvider,
    MockProvider,
    OllamaAdapter,
    OpenAIAdapter,
)
from vortex_ai_gateway.routing import (
    ModelRoute,
    ProviderRouter,
    RoutingConfigError,
    UnroutableModelError,
    build_router,
    parse_routes,
)

ROUTES = "gpt-4o=openai,gpt-*=openai,claude-*=anthropic,local/*=ollama"


def router_over(*names: str, routes: str = ROUTES, default: str | None = None) -> ProviderRouter:
    """A router whose providers are scripted mocks, one per name."""
    return ProviderRouter(
        routes=parse_routes(routes),
        providers={name: MockProvider(name=name, replies=[CannedReply(name)]) for name in names},
        default=default,
    )


# -- Parsing ----------------------------------------------------------------


def test_the_table_is_read_in_order() -> None:
    """Order is the point: a specific rule can precede a general one."""
    assert parse_routes(ROUTES) == (
        ModelRoute("gpt-4o", "openai"),
        ModelRoute("gpt-*", "openai"),
        ModelRoute("claude-*", "anthropic"),
        ModelRoute("local/*", "ollama"),
    )


def test_whitespace_and_empty_entries_are_tolerated() -> None:
    """A table wrapped across lines in a manifest still parses."""
    assert parse_routes("  gpt-4o = openai ,, claude-* =anthropic,") == (
        ModelRoute("gpt-4o", "openai"),
        ModelRoute("claude-*", "anthropic"),
    )


def test_an_empty_table_is_not_an_error() -> None:
    """No routing configured is a valid state — the mock serves instead."""
    assert parse_routes("") == ()


@pytest.mark.parametrize(
    "spec", ["gpt-4o", "=openai", "gpt-4o=", "gpt-4o:openai"], ids=lambda s: s or "empty"
)
def test_a_malformed_rule_is_rejected_with_the_rule_in_the_message(spec: str) -> None:
    """The operator has to be able to see which of twelve rules is wrong."""
    with pytest.raises(RoutingConfigError, match="pattern=provider"):
        parse_routes(spec)


@pytest.mark.parametrize(
    ("pattern", "model", "matches"),
    [
        ("gpt-4o", "gpt-4o", True),
        ("gpt-4o", "gpt-4o-mini", False),
        ("gpt-*", "gpt-4o-mini", True),
        ("local/*", "local/llama3.2", True),
        ("local/*", "llama3.2", False),
        ("claude-*", "CLAUDE-sonnet", False),
    ],
)
def test_patterns_are_globs_and_case_sensitive(pattern: str, model: str, matches: bool) -> None:
    """Case folding follows the *host's* filesystem in ``fnmatch``, which would
    make routing differ between a laptop and the container."""
    assert ModelRoute(pattern, "openai").matches(model) is matches


# -- Dispatch ---------------------------------------------------------------


def test_a_router_is_a_provider_like_any_other() -> None:
    """Routing composes through the seam rather than sitting beside it."""
    assert isinstance(router_over("openai", "anthropic", "ollama"), ChatProvider)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-4o", "openai"),
        ("gpt-4o-mini", "openai"),
        ("claude-sonnet-4-5", "anthropic"),
        ("local/llama3.2", "ollama"),
    ],
)
async def test_the_first_matching_rule_serves_the_request(model: str, expected: str) -> None:
    router = router_over("openai", "anthropic", "ollama")

    response = await router.complete(chat_request(model=model))

    assert response.choices[0].message.content == expected
    assert response.vortex is not None
    assert response.vortex.provider == expected


async def test_an_unmatched_model_is_refused_by_name() -> None:
    """A typo is a 400-class failure, not a surprise bill on whoever is first."""
    router = router_over("openai", "anthropic", "ollama")

    with pytest.raises(UnroutableModelError, match="mistral-large") as caught:
        await router.complete(chat_request(model="mistral-large"))

    assert caught.value.retryable is False
    assert "claude-*=anthropic" in str(caught.value), "the message lists the table"


async def test_a_default_provider_catches_what_no_rule_claims() -> None:
    router = router_over("openai", "anthropic", "ollama", default="ollama")

    response = await router.complete(chat_request(model="mistral-large"))

    assert response.choices[0].message.content == "ollama"


async def test_streaming_is_delegated_to_the_same_provider() -> None:
    router = router_over("openai", "anthropic", "ollama")

    chunks = [chunk async for chunk in router.stream(chat_request(model="claude-3", stream=True))]

    assert chunks[0].vortex is not None
    assert chunks[0].vortex.provider == "anthropic"


def test_an_unroutable_stream_fails_before_it_is_iterated() -> None:
    """Not an ``async def``: a failure after the ``200`` cannot carry a status."""
    router = router_over("openai", "anthropic", "ollama")

    with pytest.raises(UnroutableModelError):
        router.stream(chat_request(model="mistral-large", stream=True))


def test_a_rule_naming_a_provider_that_was_not_built_is_rejected() -> None:
    """Caught when the router is assembled, not when traffic arrives."""
    with pytest.raises(RoutingConfigError, match="anthropic"):
        router_over("openai")


async def test_closing_the_router_closes_every_provider() -> None:
    adapters = {"openai": OpenAIAdapter(api_key="k"), "ollama": OllamaAdapter()}
    router = ProviderRouter(routes=parse_routes("gpt-*=openai,local/*=ollama"), providers=adapters)
    opened = [adapter.client for adapter in adapters.values()]

    await router.aclose()

    assert all(client.is_closed for client in opened)


def test_a_router_describes_its_table_when_printed() -> None:
    assert "claude-*=anthropic" in repr(router_over("openai", "anthropic", "ollama"))


# -- Building from configuration --------------------------------------------


def settings_with(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_no_table_and_no_default_builds_no_router() -> None:
    """Which is what leaves the gateway on its mock, loudly."""
    assert build_router(settings_with()) is None


def test_only_the_providers_the_table_names_are_built() -> None:
    """An unrelated missing key cannot stop the gateway booting."""
    router = build_router(settings_with(model_routes="local/*=ollama"))

    assert router is not None
    assert set(router.providers) == {"ollama"}


def test_adapters_are_built_from_the_matching_settings() -> None:
    """The ``<name>_api_key`` / ``<name>_base_url`` convention, exercised."""
    router = build_router(
        settings_with(
            model_routes="claude-*=anthropic",
            anthropic_api_key="sk-ant-test",
            anthropic_base_url="https://anthropic.internal",
            request_timeout_seconds=12.0,
            connect_timeout_seconds=3.0,
        )
    )

    assert router is not None
    adapter = router.providers["anthropic"]
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.api_key == "sk-ant-test"
    assert adapter.base_url == "https://anthropic.internal"
    assert adapter.timeout == 12.0
    assert adapter.connect_timeout == 3.0


def test_a_routed_provider_without_its_key_stops_the_gateway_booting() -> None:
    """Better than discovering it as a 401 on the first real request."""
    with pytest.raises(RoutingConfigError, match="VORTEX_OPENAI_API_KEY"):
        build_router(settings_with(model_routes="gpt-*=openai"))


def test_a_local_runtime_needs_no_key() -> None:
    router = build_router(settings_with(model_routes="local/*=ollama"))

    assert router is not None
    assert isinstance(router.providers["ollama"], OllamaAdapter)


def test_an_unknown_provider_name_is_rejected_with_the_known_ones() -> None:
    with pytest.raises(RoutingConfigError, match="Known providers: anthropic, ollama, openai"):
        build_router(settings_with(model_routes="gemini-*=google"))


def test_a_default_provider_alone_is_enough_to_build_a_router() -> None:
    router = build_router(settings_with(default_provider="ollama"))

    assert router is not None
    assert router.routes == ()
    assert router.default == "ollama"


def test_a_default_provider_must_also_exist() -> None:
    with pytest.raises(RoutingConfigError, match="google"):
        build_router(settings_with(default_provider="google"))


# -- Through the whole app --------------------------------------------------


async def test_the_app_routes_a_request_to_the_configured_vendor() -> None:
    """End to end: config in, one vendor's wire format out, contract back."""
    settings = Settings(
        _env_file=None,
        model_routes="gpt-*=openai,claude-*=anthropic",
        openai_api_key="sk-test",
        anthropic_api_key="sk-ant-test",
    )
    answer = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "text", "text": "routed"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }

    with respx.mock(assert_all_called=False) as upstream:
        route = upstream.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=answer)
        )
        body = await _post(settings, {"model": "claude-sonnet-4-5", "messages": [_HELLO]})

    assert route.called
    assert route.calls.last.request.headers["x-api-key"] == "sk-ant-test"
    assert body["choices"][0]["message"]["content"] == "routed"
    assert body["vortex"]["provider"] == "anthropic"
    assert body["model"] == "claude-sonnet-4-5"


async def test_a_model_nothing_routes_is_a_404_the_caller_can_read() -> None:
    settings = Settings(_env_file=None, model_routes="gpt-*=openai", openai_api_key="sk-test")

    body = await _post(settings, {"model": "mistral-large", "messages": [_HELLO]}, expect=404)

    assert body["error"]["code"] == "model_not_found"
    assert body["error"]["type"] == "invalid_request_error"
    assert "mistral-large" in body["error"]["message"]


_HELLO = {"role": "user", "content": "hi"}


async def _post(settings: Settings, payload: dict[str, Any], expect: int = 200) -> dict[str, Any]:
    """POST one completion through an app built from ``settings``."""
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.post(
            "/v1/chat/completions", json=payload, headers={"authorization": "Bearer client-key"}
        )

    assert response.status_code == expect, response.text
    body: dict[str, Any] = response.json()
    return body
