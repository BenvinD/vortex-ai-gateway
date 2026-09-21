"""The Ollama embedder behind the semantic cache's ``Embedder`` seam.

Translation tests, as the adapter tests are: what was sent to ``/api/embed``,
and what came back — plus the one contract the semantic cache relies on, that
every failure is a single :class:`EmbeddingError` and never an httpx exception
or a ``KeyError`` from a body that was not what Ollama promised.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fakeredis import aioredis

from tests.test_cache import client_for
from tests.upstream import Upstream
from vortex_ai_gateway.cache import CACHE_HEADER
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.embedding import (
    DEFAULT_OLLAMA_URL,
    EMBED_PATH,
    EmbeddingError,
    OllamaEmbedder,
    build_embedder,
)
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import MockProvider
from vortex_ai_gateway.semantic import DEGRADED_EVENT, SEMANTIC_HEADER

CHAT_URL = "/v1/chat/completions"


def embed_response(*vectors: list[float]) -> httpx.Response:
    return httpx.Response(200, json={"model": "nomic-embed-text", "embeddings": list(vectors)})


def embedder_over(upstream: Upstream, **kwargs: object) -> OllamaEmbedder:
    return OllamaEmbedder("nomic-embed-text", client=upstream.client(), **kwargs)  # type: ignore[arg-type]


# --- the request ---------------------------------------------------------


async def test_posts_the_model_and_text_to_api_embed() -> None:
    upstream = Upstream(embed_response([0.1, 0.2, 0.3]))
    embedder = embedder_over(upstream, base_url="http://ollama.test:11434/")

    vector = await embedder.embed("user: hello")

    assert list(vector) == [0.1, 0.2, 0.3]
    assert upstream.url == f"http://ollama.test:11434{EMBED_PATH}"
    assert upstream.sent == {"model": "nomic-embed-text", "input": "user: hello"}


async def test_base_url_defaults_to_the_local_ollama() -> None:
    upstream = Upstream(embed_response([1.0]))
    embedder = embedder_over(upstream)

    await embedder.embed("x")

    assert upstream.url == f"{DEFAULT_OLLAMA_URL}{EMBED_PATH}"


def test_a_model_name_is_required() -> None:
    with pytest.raises(ValueError, match="model"):
        OllamaEmbedder("")


# --- every failure is one error ------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "fragment"),
    [
        (httpx.ConnectError("refused"), "ConnectError"),
        (httpx.ReadTimeout("slow"), "ReadTimeout"),
        (httpx.Response(404, json={"error": "model 'nope' not found"}), "404"),
        (httpx.Response(500, text="boom"), "500"),
        (httpx.Response(200, text="not json"), "not JSON"),
        (httpx.Response(200, json={"embeddings": []}), "no embedding"),
        (httpx.Response(200, json={"embedding": [1.0]}), "no embedding"),
        (httpx.Response(200, json=[1.0, 2.0]), "no embedding"),
        (httpx.Response(200, json={"embeddings": ["a", "b"]}), "not a list of numbers"),
        (httpx.Response(200, json={"embeddings": [[1.0, "b"]]}), "not a list of numbers"),
    ],
    ids=[
        "connect-error",
        "timeout",
        "404-unknown-model",
        "500",
        "body-not-json",
        "empty-embeddings",
        "singular-legacy-key",
        "body-not-an-object",
        "row-not-a-list",
        "row-not-numbers",
    ],
)
async def test_every_failure_is_an_embedding_error(
    outcome: httpx.Response | Exception, fragment: str
) -> None:
    """The semantic cache catches one thing; make sure that is all there is."""
    embedder = embedder_over(Upstream(outcome))

    with pytest.raises(EmbeddingError, match=fragment):
        await embedder.embed("x")


async def test_a_transport_error_keeps_its_cause() -> None:
    cause = httpx.ConnectError("refused")
    embedder = embedder_over(Upstream(cause))

    with pytest.raises(EmbeddingError) as info:
        await embedder.embed("x")

    assert info.value.__cause__ is cause


# --- client ownership ----------------------------------------------------


async def test_an_injected_client_is_not_closed() -> None:
    client = Upstream(embed_response([1.0])).client()
    embedder = OllamaEmbedder("m", client=client)

    await embedder.aclose()

    assert not client.is_closed


async def test_an_owned_client_is_opened_lazily_and_closed() -> None:
    embedder = OllamaEmbedder("m")
    assert embedder._client is None

    opened = embedder.client
    assert not opened.is_closed
    await embedder.aclose()

    assert opened.is_closed
    assert embedder._client is None


# --- from settings -------------------------------------------------------


def test_no_model_means_no_embedder() -> None:
    assert build_embedder(Settings(_env_file=None)) is None


def test_model_alone_targets_the_default_ollama() -> None:
    embedder = build_embedder(Settings(_env_file=None, embedding_model="nomic-embed-text"))

    assert isinstance(embedder, OllamaEmbedder)
    assert embedder.model == "nomic-embed-text"
    assert embedder.base_url == DEFAULT_OLLAMA_URL


def test_base_url_falls_back_to_the_chat_adapters_ollama() -> None:
    """One Ollama, configured once, serves both the chat and the embedding."""
    settings = Settings(
        _env_file=None,
        embedding_model="m",
        ollama_base_url="http://shared:11434",
    )
    embedder = build_embedder(settings)

    assert isinstance(embedder, OllamaEmbedder)
    assert embedder.base_url == "http://shared:11434"


def test_embedding_base_url_wins_over_the_chat_adapters() -> None:
    settings = Settings(
        _env_file=None,
        embedding_model="m",
        ollama_base_url="http://chat:11434",
        embedding_base_url="http://embed:11434/",
    )
    embedder = build_embedder(settings)

    assert isinstance(embedder, OllamaEmbedder)
    assert embedder.base_url == "http://embed:11434"


def test_timeout_comes_from_settings() -> None:
    settings = Settings(_env_file=None, embedding_model="m", request_timeout_seconds=7.5)
    embedder = build_embedder(settings)

    assert isinstance(embedder, OllamaEmbedder)
    assert embedder.timeout == 7.5


# --- through create_app --------------------------------------------------


def test_create_app_builds_the_embedder_only_when_the_tier_is_on() -> None:
    off = create_app(
        settings=Settings(_env_file=None, embedding_model="m"),
        provider=MockProvider(),
    )
    on = create_app(
        settings=Settings(_env_file=None, embedding_model="m", semantic_cache_enabled=True),
        provider=MockProvider(),
    )

    assert not off.state.semantic_cache.configured
    assert on.state.semantic_cache.configured


async def test_enabled_with_a_model_but_no_server_serves_uncached(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The tier is on and the embedder is real; Ollama is simply not there.

    The exact tier has Redis, so it *looks and misses* and the semantic tier
    is consulted (ADR-024); the embedder cannot connect; the request is served
    from the provider as a semantic ``MISS`` with one degraded warning — not a
    500, and not the "no embedder configured" warning, because one *is*.
    """
    settings = Settings(
        _env_file=None,
        cache_enabled=True,
        embedding_model="nomic-embed-text",
        embedding_base_url="http://127.0.0.1:9",  # discard port; nothing listens
        semantic_cache_enabled=True,
    )
    app = create_app(settings=settings, provider=MockProvider(), redis=aioredis.FakeRedis())
    async with client_for(app) as client:
        response = await client.post(
            CHAT_URL, json={"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        )

    assert response.status_code == 200
    assert response.headers[CACHE_HEADER] == "MISS"
    assert response.headers[SEMANTIC_HEADER] == "MISS"
    assert app.state.semantic_cache.stats.errors == 1
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert DEGRADED_EVENT in events
    assert "semantic cache enabled but no embedder configured; running without it" not in events
