"""Tests for the semantic cache's lookup pipeline.

The embedder is scripted — it returns the vector the test says a text has —
because what is under test is everything *after* the embedding: normalising,
the cosine search, the threshold, and which namespace the search happens in.
Vectors are picked so their cosines are known by construction: ``[1, 0]``
against ``[cos θ, sin θ]`` scores exactly ``cos θ``.
"""

import json
import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from fakeredis import aioredis
from fastapi import FastAPI

from tests.test_cache import client_for
from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.cache import BYPASS_HEADER, CACHE_HEADER
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest, ChatCompletionResponse
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.keys import KeyStore
from vortex_ai_gateway.providers import CannedReply, MockProvider
from vortex_ai_gateway.providers.errors import ProviderUnavailable
from vortex_ai_gateway.semantic import (
    SEMANTIC_HEADER,
    SEMANTIC_SCORE_HEADER,
    SemanticCache,
    SemanticCacheConfigError,
    VectorIndex,
    prompt_text,
    shape_digest,
    unit,
)

CHAT_URL = "/v1/chat/completions"

PRINCIPAL = Principal(key_id="abc")


def body(**overrides: object) -> dict[str, object]:
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "what is the capital of france"}],
    } | overrides


def request_for(**overrides: object) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(body(**overrides))


def asking(text: str, **overrides: object) -> ChatCompletionRequest:
    """A request whose only user turn is ``text``."""
    return request_for(messages=[{"role": "user", "content": text}], **overrides)


def reply(text: str) -> ChatCompletionResponse:
    """A response saying ``text``. Compare with :func:`said`, not ``==``:
    every instance gets its own ``id`` and ``created``."""
    return ChatCompletionResponse.model_validate(
        {
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }
    )


def said(response: ChatCompletionResponse | None) -> str | None:
    return None if response is None else response.choices[0].message.content


def at_angle(degrees: float) -> list[float]:
    """A 2-D vector whose cosine against ``[1, 0]`` is ``cos(degrees)``."""
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)]


class ScriptedEmbedder:
    """Returns whatever vector the test assigned to a text.

    The vector is looked up by the *rendered* prompt, so a test that scripts
    ``"user: hello"`` is also asserting what the cache chose to embed.
    """

    def __init__(self, vectors: dict[str, Sequence[float]]) -> None:
        self.vectors = vectors
        self.calls: list[str] = []

    async def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        return self.vectors[text]


class BrokenEmbedder:
    async def embed(self, text: str) -> Sequence[float]:
        raise ConnectionError("embedding service is down")


def cache_for(
    vectors: dict[str, Sequence[float]], **overrides: object
) -> tuple[SemanticCache, ScriptedEmbedder]:
    embedder = ScriptedEmbedder(vectors)
    return SemanticCache(embedder, **overrides), embedder  # type: ignore[arg-type]


# --- normalising --------------------------------------------------------------


def test_unit_scales_to_length_one_as_float32() -> None:
    scaled = unit([3.0, 4.0])
    assert scaled.dtype == np.float32
    assert scaled.tolist() == pytest.approx([0.6, 0.8])
    assert float(np.linalg.norm(scaled)) == pytest.approx(1.0)


def test_a_unit_vector_is_left_alone() -> None:
    assert unit([0.0, 1.0]).tolist() == [0.0, 1.0]


@pytest.mark.parametrize(
    "vector",
    [
        [0.0, 0.0],  # no direction
        [],  # nothing at all
        [[1.0, 0.0]],  # a matrix, not a vector
        [float("nan"), 1.0],  # a norm that compares False with everything
        [float("inf"), 1.0],
    ],
    ids=["zero", "empty", "matrix", "nan", "inf"],
)
def test_a_vector_with_no_direction_is_refused(vector: list[object]) -> None:
    with pytest.raises(ValueError):
        unit(vector)  # type: ignore[arg-type]


# --- what gets embedded -------------------------------------------------------


def test_the_prompt_is_every_turn_with_its_role() -> None:
    request = request_for(
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "bye"},
        ]
    )
    assert prompt_text(request) == "system: be brief\nuser: hello\nassistant: hi\nuser: bye"


def test_text_parts_render_the_same_as_a_string() -> None:
    """How a client split the content is not part of the question."""
    as_string = asking("hello world")
    as_parts = request_for(
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
            }
        ]
    )
    assert prompt_text(as_string) == prompt_text(as_parts) == "user: hello world"


def test_an_assistant_turn_with_only_tool_calls_renders_empty() -> None:
    request = request_for(
        messages=[
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ]
    )
    assert prompt_text(request) == "user: weather?\nassistant: \ntool: sunny"


def test_non_text_content_cannot_be_embedded() -> None:
    """An image with the same caption is not the same question."""
    request = request_for(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                ],
            }
        ]
    )
    assert prompt_text(request) is None


# --- the shape: everything that is not the words ------------------------------


def test_the_words_are_not_in_the_shape() -> None:
    assert shape_digest(asking("hello")) == shape_digest(asking("goodbye"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "gpt-4o"},
        {"temperature": 0.2},
        {"seed": 7},
        {"n": 2},
        {"tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]},
    ],
    ids=["model", "temperature", "seed", "n", "tools"],
)
def test_generation_parameters_change_the_shape(overrides: dict[str, object]) -> None:
    assert shape_digest(request_for()) != shape_digest(request_for(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"user": "u1"},
        {"metadata": {"trace": "t"}},
        {"stream": True, "stream_options": {"include_usage": True}},
    ],
    ids=["user", "metadata", "stream-and-options"],
)
def test_non_semantic_fields_do_not_change_the_shape(overrides: dict[str, object]) -> None:
    assert shape_digest(request_for()) == shape_digest(request_for(**overrides))


# --- the index ----------------------------------------------------------------


def test_an_empty_index_has_no_nearest() -> None:
    index = VectorIndex()
    assert len(index) == 0
    assert index.dimension is None
    assert index.nearest(unit([1.0, 0.0])) is None


def test_nearest_is_the_highest_cosine() -> None:
    index = VectorIndex()
    index.add(unit(at_angle(90)), reply("far"))
    index.add(unit(at_angle(10)), reply("near"))
    index.add(unit(at_angle(45)), reply("middling"))

    match = index.nearest(unit([1.0, 0.0]))
    assert match is not None
    assert said(match.response) == "near"
    assert match.score == pytest.approx(math.cos(math.radians(10)), abs=1e-6)


def test_a_negative_cosine_is_still_the_nearest_when_nothing_is_closer() -> None:
    """The score is a cosine, not a distance: opposite vectors score -1."""
    index = VectorIndex()
    index.add(unit([-1.0, 0.0]), reply("opposite"))
    match = index.nearest(unit([1.0, 0.0]))
    assert match is not None
    assert match.score == pytest.approx(-1.0)


def test_every_row_survives_the_index_growing() -> None:
    """The bug this guards: a copy on growth that drops or reorders rows."""
    index = VectorIndex()
    count = VectorIndex.INITIAL_CAPACITY * 3 + 1
    for i in range(count):
        # Distinct directions around a circle, none closer to each other than
        # to themselves.
        index.add(unit(at_angle(i * 360 / count)), reply(str(i)))
    assert len(index) == count

    for i in range(count):
        match = index.nearest(unit(at_angle(i * 360 / count)))
        assert match is not None
        assert said(match.response) == str(i), i
        assert match.score == pytest.approx(1.0, abs=1e-6)


def test_a_different_dimension_is_refused_on_add_and_on_search() -> None:
    index = VectorIndex()
    index.add(unit([1.0, 0.0]), reply("2d"))
    assert index.dimension == 2
    with pytest.raises(ValueError, match="dimension 3"):
        index.add(unit([1.0, 0.0, 0.0]), reply("3d"))
    with pytest.raises(ValueError, match="dimension 3"):
        index.nearest(unit([1.0, 0.0, 0.0]))
    assert len(index) == 1


# --- the pipeline: embed → search → threshold → decide ------------------------


async def test_a_close_enough_paraphrase_is_a_hit() -> None:
    cache, embedder = cache_for(
        {
            "user: what is the capital of france": [1.0, 0.0],
            "user: capital of france?": at_angle(10),  # cos ≈ 0.985
        },
        threshold=0.95,
    )

    first = await cache.lookup(
        PRINCIPAL, asking("what is the capital of france"), path=CHAT_URL, headers={}
    )
    assert first.outcome == "MISS"
    assert first.score is None  # nothing to be near yet
    await cache.store(first, reply("Paris"))

    second = await cache.lookup(PRINCIPAL, asking("capital of france?"), path=CHAT_URL, headers={})
    assert second.outcome == "HIT"
    assert said(second.response) == "Paris"
    assert second.score == pytest.approx(math.cos(math.radians(10)), abs=1e-6)
    assert embedder.calls == ["user: what is the capital of france", "user: capital of france?"]


async def test_a_near_miss_is_a_miss_that_says_how_near() -> None:
    """The score on a miss is the evidence ADR-005 needs to pick a threshold."""
    cache, _ = cache_for(
        {"user: a": [1.0, 0.0], "user: b": at_angle(30)},  # cos ≈ 0.866
        threshold=0.95,
    )
    first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(first, reply("A"))

    second = await cache.lookup(PRINCIPAL, asking("b"), path=CHAT_URL, headers={})
    assert second.outcome == "MISS"
    assert second.response is None
    assert second.score == pytest.approx(math.cos(math.radians(30)), abs=1e-6)


async def test_the_threshold_is_what_separates_hit_from_miss() -> None:
    """The same pair of vectors, decided both ways by the threshold alone."""
    vectors = {"user: a": [1.0, 0.0], "user: b": at_angle(30)}
    for threshold, expected in ((0.9, "MISS"), (0.8, "HIT")):
        cache, _ = cache_for(vectors, threshold=threshold)
        first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
        await cache.store(first, reply("A"))
        second = await cache.lookup(PRINCIPAL, asking("b"), path=CHAT_URL, headers={})
        assert second.outcome == expected, threshold


async def test_the_embedder_need_not_normalise() -> None:
    """A vector ten times longer points the same way and scores the same."""
    cache, _ = cache_for({"user: a": [10.0, 0.0], "user: b": [0.5, 0.0]})
    first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(first, reply("A"))
    second = await cache.lookup(PRINCIPAL, asking("b"), path=CHAT_URL, headers={})
    assert second.outcome == "HIT"
    assert second.score == pytest.approx(1.0)


async def test_a_hit_is_not_stored_again() -> None:
    """Storing on a hit would fill the index with copies of one answer."""
    cache, _ = cache_for({"user: a": [1.0, 0.0]})
    first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(first, reply("A"))

    hit = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    assert hit.outcome == "HIT"
    assert hit.index is None
    await cache.store(hit, reply("A again"))

    namespace = cache.namespace_for(PRINCIPAL, asking("a"), CHAT_URL)
    assert len(cache.index_for(namespace)) == 1
    assert cache.stats.stores == 1


async def test_the_lookup_embeds_once_and_the_store_reuses_it() -> None:
    cache, embedder = cache_for({"user: a": [1.0, 0.0]})
    lookup = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(lookup, reply("A"))
    assert embedder.calls == ["user: a"]


# --- namespaces: who is compared with whom -------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"model": "gpt-4o"}, {"temperature": 0.0}, {"seed": 1}],
    ids=["model", "temperature", "seed"],
)
async def test_the_same_words_under_a_different_shape_do_not_meet(
    overrides: dict[str, object],
) -> None:
    cache, _ = cache_for({"user: a": [1.0, 0.0]})
    first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(first, reply("A"))

    other = await cache.lookup(PRINCIPAL, asking("a", **overrides), path=CHAT_URL, headers={})
    assert other.outcome == "MISS"
    assert other.score is None


async def test_the_same_words_on_a_different_route_do_not_meet() -> None:
    cache, _ = cache_for({"user: a": [1.0, 0.0]})
    first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(first, reply("A"))
    other = await cache.lookup(PRINCIPAL, asking("a"), path="/v1/other", headers={})
    assert other.outcome == "MISS"


async def test_entries_are_not_shared_between_keys() -> None:
    cache, _ = cache_for({"user: a": [1.0, 0.0]})
    first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(first, reply("A"))

    other = await cache.lookup(Principal(key_id="xyz"), asking("a"), path=CHAT_URL, headers={})
    assert other.outcome == "MISS"


async def test_the_global_scope_shares_one_namespace() -> None:
    cache, _ = cache_for({"user: a": [1.0, 0.0]}, scope="global")
    first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(first, reply("A"))

    other = await cache.lookup(Principal(key_id="xyz"), asking("a"), path=CHAT_URL, headers={})
    assert other.outcome == "HIT"


def test_the_namespace_is_tenant_route_and_shape() -> None:
    request = asking("a")
    assert (
        cache_for({})[0].namespace_for(PRINCIPAL, request, CHAT_URL)
        == f"abc:{CHAT_URL}:{shape_digest(request)}"
    )
    assert (
        cache_for({}, scope="global")[0].namespace_for(PRINCIPAL, request, CHAT_URL)
        == f"global:{CHAT_URL}:{shape_digest(request)}"
    )


# --- bypasses: decided before the embedder is called ---------------------------


@pytest.mark.parametrize(
    ("request_", "headers"),
    [
        (asking("a", stream=True), {}),
        (asking("a"), {BYPASS_HEADER: "1"}),
        (
            request_for(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "a"},
                            {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
                        ],
                    }
                ]
            ),
            {},
        ),
    ],
    ids=["stream", "bypass-header", "image"],
)
async def test_a_bypass_never_reaches_the_embedder(
    request_: ChatCompletionRequest, headers: dict[str, str]
) -> None:
    cache, embedder = cache_for({})
    lookup = await cache.lookup(PRINCIPAL, request_, path=CHAT_URL, headers=headers)
    assert lookup.outcome == "BYPASS"
    assert lookup.index is None
    assert embedder.calls == []
    assert cache.stats.bypasses == 1

    await cache.store(lookup, reply("A"))
    assert cache.stats.stores == 0


async def test_no_embedder_means_no_outcome() -> None:
    """A gateway without an embedding model has no semantic cache to report on."""
    cache = SemanticCache(None)
    lookup = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    assert lookup.outcome is None
    await cache.store(lookup, reply("A"))
    assert cache.stats.snapshot() == {
        "hits": 0,
        "misses": 0,
        "bypasses": 0,
        "stores": 0,
        "errors": 0,
        "hit_rate": 0.0,
    }


# --- failing open --------------------------------------------------------------


async def test_a_broken_embedder_is_a_miss_that_stores_nothing() -> None:
    cache = SemanticCache(BrokenEmbedder())
    lookup = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    assert lookup.outcome == "MISS"
    assert lookup.index is None
    await cache.store(lookup, reply("A"))
    assert cache.stats.errors == 1
    assert cache.stats.stores == 0
    assert cache.stats.misses == 0  # an error is not a miss in the hit rate


async def test_a_zero_vector_is_a_miss_that_stores_nothing() -> None:
    """A vector with no direction would be ``nan`` against every threshold."""
    cache, _ = cache_for({"user: a": [0.0, 0.0]})
    lookup = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    assert lookup.outcome == "MISS"
    assert lookup.index is None
    assert cache.stats.errors == 1


async def test_an_embedder_that_changes_dimension_does_not_poison_the_index() -> None:
    cache, _ = cache_for({"user: a": [1.0, 0.0], "user: b": [1.0, 0.0, 0.0]})
    first = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(first, reply("A"))

    second = await cache.lookup(PRINCIPAL, asking("b"), path=CHAT_URL, headers={})
    assert second.outcome == "MISS"
    assert second.index is None
    await cache.store(second, reply("B"))

    namespace = cache.namespace_for(PRINCIPAL, asking("a"), CHAT_URL)
    assert len(cache.index_for(namespace)) == 1
    assert cache.stats.errors == 1


@pytest.mark.parametrize("threshold", [0.0, -0.5, 1.5])
def test_a_threshold_that_is_not_a_similarity_is_rejected(threshold: float) -> None:
    with pytest.raises(SemanticCacheConfigError):
        SemanticCache(None, threshold=threshold)


async def test_the_counters_follow_what_actually_happened() -> None:
    cache, _ = cache_for(
        {"user: a": [1.0, 0.0], "user: b": at_angle(60), "user: c": at_angle(5)},
        threshold=0.9,
    )
    a = await cache.lookup(PRINCIPAL, asking("a"), path=CHAT_URL, headers={})
    await cache.store(a, reply("A"))
    b = await cache.lookup(PRINCIPAL, asking("b"), path=CHAT_URL, headers={})
    await cache.store(b, reply("B"))
    await cache.lookup(PRINCIPAL, asking("c"), path=CHAT_URL, headers={})
    await cache.lookup(PRINCIPAL, asking("a", stream=True), path=CHAT_URL, headers={})

    assert cache.stats.snapshot() == {
        "hits": 1,
        "misses": 2,
        "bypasses": 1,
        "stores": 2,
        "errors": 0,
        "hit_rate": round(1 / 3, 4),
    }


# --- over HTTP: exact first, semantic second ----------------------------------


def gateway(
    vectors: dict[str, Sequence[float]],
    *,
    redis: aioredis.FakeRedis | None,
    provider: MockProvider | None = None,
    embedder: ScriptedEmbedder | None = None,
    **overrides: object,
) -> tuple[FastAPI, ScriptedEmbedder, MockProvider]:
    """A gateway with both tiers configured as the test needs them.

    ``redis=None`` is a gateway with *no exact tier at all*, which is a
    different case from an exact tier that missed: the semantic tier must run
    in both.
    """
    embedder = embedder or ScriptedEmbedder(vectors)
    provider = provider or MockProvider()
    settings = Settings(
        _env_file=None,
        cache_enabled=redis is not None,
        semantic_cache_enabled=True,
        **overrides,
    )
    app = create_app(settings=settings, provider=provider, redis=redis, embedder=embedder)
    return app, embedder, provider


#: Cosines against ``PROMPTS["exact"]``: the paraphrase clears 0.95, the
#: unrelated question does not.
PROMPTS: dict[str, Sequence[float]] = {
    "user: what is the capital of france": [1.0, 0.0],
    "user: capital of france?": at_angle(10),
    "user: how tall is the eiffel tower": at_angle(80),
}


async def test_an_exact_hit_never_pays_for_an_embedding() -> None:
    """Cheapest first: the hash answered, so the embedder is not asked."""
    app, embedder, provider = gateway(PROMPTS, redis=aioredis.FakeRedis())
    async with client_for(app) as client:
        first = await client.post(CHAT_URL, json=body())
        second = await client.post(CHAT_URL, json=body())

    assert first.headers[CACHE_HEADER] == "MISS"
    assert first.headers[SEMANTIC_HEADER] == "MISS"
    assert SEMANTIC_SCORE_HEADER not in first.headers  # nothing to be near yet
    assert second.headers[CACHE_HEADER] == "HIT"
    assert second.headers[SEMANTIC_HEADER] == "BYPASS"
    assert embedder.calls == ["user: what is the capital of france"]
    assert len(provider.received_requests) == 1


async def test_a_paraphrase_is_served_by_the_semantic_tier_and_promoted() -> None:
    """The semantic tier answers once; after that the exact tier answers."""
    app, embedder, provider = gateway(PROMPTS, redis=aioredis.FakeRedis())
    paraphrase = body(messages=[{"role": "user", "content": "capital of france?"}])
    async with client_for(app) as client:
        original = await client.post(CHAT_URL, json=body())
        first = await client.post(CHAT_URL, json=paraphrase)
        second = await client.post(CHAT_URL, json=paraphrase)

    assert first.headers[CACHE_HEADER] == "MISS"
    assert first.headers[SEMANTIC_HEADER] == "HIT"
    assert float(first.headers[SEMANTIC_SCORE_HEADER]) == pytest.approx(
        math.cos(math.radians(10)), abs=1e-4
    )
    assert first.json()["choices"] == original.json()["choices"]

    # Promoted: the repeat is an exact hit and the embedder was not asked again.
    assert second.headers[CACHE_HEADER] == "HIT"
    assert second.headers[SEMANTIC_HEADER] == "BYPASS"
    assert embedder.calls == [
        "user: what is the capital of france",
        "user: capital of france?",
    ]
    assert len(provider.received_requests) == 1
    assert app.state.semantic_cache.stats.snapshot()["stores"] == 1


async def test_a_different_question_misses_both_tiers_and_says_how_near() -> None:
    app, _, provider = gateway(PROMPTS, redis=aioredis.FakeRedis())
    other = body(messages=[{"role": "user", "content": "how tall is the eiffel tower"}])
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body())
        response = await client.post(CHAT_URL, json=other)

    assert response.headers[CACHE_HEADER] == "MISS"
    assert response.headers[SEMANTIC_HEADER] == "MISS"
    assert float(response.headers[SEMANTIC_SCORE_HEADER]) == pytest.approx(
        math.cos(math.radians(80)), abs=1e-4
    )
    assert len(provider.received_requests) == 2
    assert app.state.semantic_cache.stats.snapshot()["stores"] == 2


async def test_the_semantic_tier_runs_without_an_exact_tier() -> None:
    """No Redis is "no exact cache", not "no caching": the second tier still answers."""
    app, _, provider = gateway(PROMPTS, redis=None)
    paraphrase = body(messages=[{"role": "user", "content": "capital of france?"}])
    async with client_for(app) as client:
        first = await client.post(CHAT_URL, json=body())
        second = await client.post(CHAT_URL, json=paraphrase)

    assert CACHE_HEADER not in first.headers
    assert first.headers[SEMANTIC_HEADER] == "MISS"
    assert CACHE_HEADER not in second.headers
    assert second.headers[SEMANTIC_HEADER] == "HIT"
    assert len(provider.received_requests) == 1


@pytest.mark.parametrize(
    ("request_body", "headers", "overrides"),
    [
        (body(stream=True), {}, {}),
        (body(), {BYPASS_HEADER: "1"}, {}),
        (body(), {}, {"cache_ttls": f"{CHAT_URL}=0"}),
    ],
    ids=["stream", "bypass-header", "zero-ttl-route"],
)
async def test_an_exact_bypass_is_a_semantic_bypass_too(
    request_body: dict[str, object], headers: dict[str, str], overrides: dict[str, object]
) -> None:
    """A bypass is about the request, and the second tier does not re-decide it."""
    app, embedder, _ = gateway(PROMPTS, redis=aioredis.FakeRedis(), **overrides)
    async with client_for(app) as client:
        response = await client.post(CHAT_URL, json=request_body, headers=headers)

    assert response.headers[CACHE_HEADER] == "BYPASS"
    assert response.headers[SEMANTIC_HEADER] == "BYPASS"
    assert embedder.calls == []
    assert app.state.semantic_cache.stats.snapshot()["bypasses"] == 1


async def test_a_failed_request_is_stored_in_neither_tier() -> None:
    provider = MockProvider(
        replies=[ProviderUnavailable("down", provider="mock"), CannedReply("ok")]
    )
    app, embedder, _ = gateway(PROMPTS, redis=aioredis.FakeRedis(), provider=provider)
    async with client_for(app) as client:
        failed = await client.post(CHAT_URL, json=body())
        retried = await client.post(CHAT_URL, json=body())

    assert failed.status_code == 502
    assert embedder.calls == ["user: what is the capital of france"] * 2
    assert retried.headers[CACHE_HEADER] == "MISS"
    assert retried.headers[SEMANTIC_HEADER] == "MISS"
    assert app.state.semantic_cache.stats.snapshot()["stores"] == 1


async def test_a_semantic_hit_is_a_request_that_bought_nothing(tmp_path: Path) -> None:
    """Metered and semantic combined: the tier that found it does not change the price."""
    store = KeyStore(tmp_path / "keys.sqlite3")
    minted = store.create(name="semantic", rpm=100, tpm=100_000)
    app, _, _ = gateway(
        PROMPTS,
        redis=aioredis.FakeRedis(),
        key_db_path=str(store.path),
        metering_enabled=True,
    )
    auth = {"Authorization": f"Bearer {minted.token}"}
    paraphrase = body(messages=[{"role": "user", "content": "capital of france?"}])
    async with client_for(app) as client:
        first = await client.post(CHAT_URL, json=body(), headers=auth)
        second = await client.post(CHAT_URL, json=paraphrase, headers=auth)

    assert second.headers[SEMANTIC_HEADER] == "HIT"
    report = await app.state.meter.ledger.report(minted.record.key_id, days=1)
    assert report.total_requests == 2
    assert report.total_tokens == first.json()["usage"]["total_tokens"]


async def test_enabled_without_an_embedder_warns_and_runs_without(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The difference between "off" and "silently off" is one log line."""
    settings = Settings(_env_file=None, semantic_cache_enabled=True)
    app = create_app(settings=settings, provider=MockProvider())
    assert not app.state.semantic_cache.configured
    async with client_for(app) as client:
        response = await client.post(CHAT_URL, json=body())

    assert SEMANTIC_HEADER not in response.headers
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert "semantic cache enabled but no embedder configured; running without it" in events


async def test_disabled_ignores_the_embedder() -> None:
    settings = Settings(_env_file=None, semantic_cache_enabled=False)
    embedder = ScriptedEmbedder(PROMPTS)
    app = create_app(settings=settings, provider=MockProvider(), embedder=embedder)
    async with client_for(app) as client:
        response = await client.post(CHAT_URL, json=body())

    assert SEMANTIC_HEADER not in response.headers
    assert embedder.calls == []


async def test_the_access_line_carries_the_tier_and_the_score(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every score lands in the log, which is where the threshold gets tuned."""
    app, _, _ = gateway(PROMPTS, redis=aioredis.FakeRedis())
    paraphrase = body(messages=[{"role": "user", "content": "capital of france?"}])
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body())
        await client.post(CHAT_URL, json=paraphrase)
        await client.post(CHAT_URL, json=paraphrase)

    completed = [
        record
        for record in (json.loads(line) for line in capsys.readouterr().out.splitlines())
        if record.get("event") == "request completed"
    ]
    assert [(r["cache"], r["semantic_cache"]) for r in completed] == [
        ("MISS", "MISS"),
        ("MISS", "HIT"),
        ("HIT", "BYPASS"),
    ]
    assert "semantic_score" not in completed[0]
    assert completed[1]["semantic_score"] == pytest.approx(math.cos(math.radians(10)), abs=1e-4)
    assert "semantic_score" not in completed[2]


def test_the_settings_reach_the_cache() -> None:
    settings = Settings(
        _env_file=None,
        semantic_cache_enabled=True,
        semantic_cache_threshold=0.8,
        cache_scope="global",
    )
    cache = SemanticCache.from_settings(settings, ScriptedEmbedder({}))
    assert cache.configured
    assert cache.threshold == 0.8
    assert cache.scope == "global"
