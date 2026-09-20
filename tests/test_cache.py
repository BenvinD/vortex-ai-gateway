"""Tests for the exact response cache.

Three layers, and the middle one is where the bugs live. The digest tests pin
what counts as "the same question"; the HTTP tests prove the answer is reused
without the provider being asked again; and the metered tests prove a hit is
free — which is the assertion that fails silently, because a cache that bills
the caller for tokens it never bought looks perfect from the header.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakeredis import aioredis
from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.test_spend import Clock
from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.cache import (
    BYPASS_HEADER,
    CACHE_HEADER,
    CacheConfigError,
    ResponseCache,
    canonical_json,
    parse_cache_ttls,
    request_digest,
)
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest, ChatCompletionResponse
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.keys import KeyStore
from vortex_ai_gateway.providers import CannedReply, MockProvider
from vortex_ai_gateway.providers.errors import ProviderUnavailable
from vortex_ai_gateway.ratelimit import RateLimiter

CHAT_URL = "/v1/chat/completions"

#: Any well-formed key is accepted while no allow-list is configured.
AUTH = {"Authorization": "Bearer test-key"}


def body(**overrides: object) -> dict[str, object]:
    """A minimal valid request body, plus any overrides."""
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
    } | overrides


def request_for(**overrides: object) -> ChatCompletionRequest:
    """The same body, validated — which is what the cache actually hashes."""
    return ChatCompletionRequest.model_validate(body(**overrides))


@pytest.fixture
def redis() -> aioredis.FakeRedis:
    return aioredis.FakeRedis()


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


def build(
    redis: aioredis.FakeRedis | None,
    provider: MockProvider | None = None,
    **overrides: object,
) -> FastAPI:
    """A gateway with the cache configured as the test needs it."""
    settings = Settings(_env_file=None, cache_enabled=redis is not None, **overrides)
    return create_app(settings=settings, provider=provider or MockProvider(), redis=redis)


def client_for(app: FastAPI) -> httpx.AsyncClient:
    """An async client over the app, so a test can await Redis in the same loop.

    `TestClient` runs the app in a portal with an event loop of its own, and the
    fake Redis these tests assert against lives in this one.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
        headers=AUTH,
    )


async def entries(redis: aioredis.FakeRedis) -> list[bytes]:
    """Every cache key currently in Redis."""
    return sorted(await redis.keys("vortex:cache:*"))


# --- what counts as the same question ----------------------------------------


def test_field_order_does_not_change_the_digest() -> None:
    """A client library's JSON key order is not part of the question."""
    ordered = ChatCompletionRequest.model_validate(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}
    )
    reversed_ = ChatCompletionRequest.model_validate(
        {"messages": [{"role": "user", "content": "ping"}], "model": "gpt-4o-mini"}
    )

    assert request_digest(ordered) == request_digest(reversed_)


def test_an_omitted_field_hashes_as_its_default() -> None:
    """Sending `temperature: 1.0` and omitting it ask for the same thing."""
    assert request_digest(request_for()) == request_digest(request_for(temperature=1.0))


def test_one_token_different_is_a_different_question() -> None:
    """The whole point: near-identical must not collide."""
    other = ChatCompletionRequest.model_validate(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "pong"}]}
    )

    assert request_digest(request_for()) != request_digest(other)


@pytest.mark.parametrize(
    "overrides",
    [
        {"user": "u-1"},
        {"metadata": {"team": "search"}},
        {"stream": False},
        {"stream": True},
    ],
    ids=["user", "metadata", "stream-false", "stream-true"],
)
def test_non_semantic_fields_do_not_change_the_digest(overrides: dict[str, Any]) -> None:
    """Framing and caller-defined tags cannot change a generated token."""
    assert request_digest(request_for()) == request_digest(request_for(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "gpt-4o"},
        {"temperature": 0.3},
        {"seed": 7},
        {"n": 2},
        {"max_completion_tokens": 16},
        {"stop": ["\n"]},
        {"tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]},
    ],
    ids=["model", "temperature", "seed", "n", "max-tokens", "stop", "tools"],
)
def test_generation_parameters_are_in_the_digest(overrides: dict[str, Any]) -> None:
    """Anything that can change the output has to change the key."""
    assert request_digest(request_for()) != request_digest(request_for(**overrides))


def test_the_canonical_form_is_sorted_and_compact() -> None:
    """Stability is the property; sorted keys and no spacing are how it is had."""
    serialised = canonical_json(request_for())

    assert " " not in serialised.replace('"ping"', "")
    assert list(json.loads(serialised)) == sorted(json.loads(serialised))


# --- the per-route TTL table --------------------------------------------------


def test_ttls_parse_per_route() -> None:
    assert parse_cache_ttls(" /v1/chat/completions = 600 , /v1/embeddings=30 ") == {
        "/v1/chat/completions": 600,
        "/v1/embeddings": 30,
    }


def test_an_empty_table_is_empty_rather_than_an_error() -> None:
    assert parse_cache_ttls("") == {}


def test_a_zero_ttl_is_a_rule_not_an_absence() -> None:
    """`path=0` says "never cache this route", which a default cannot say."""
    cache = ResponseCache(None, default_ttl=300, ttls=parse_cache_ttls("/v1/chat/completions=0"))

    assert cache.ttl_for("/v1/chat/completions") == 0
    assert cache.ttl_for("/v1/embeddings") == 300


@pytest.mark.parametrize(
    "spec", ["/v1/chat/completions", "=600", "/v1/chat/completions=", "/v1/x=soon", "/v1/x=-1"]
)
def test_a_malformed_ttl_rule_is_rejected(spec: str) -> None:
    """A TTL that cannot be read must not silently become the default."""
    with pytest.raises(CacheConfigError):
        parse_cache_ttls(spec)


# --- hits and misses over HTTP ------------------------------------------------


async def test_an_identical_request_is_served_from_the_cache(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """The second request never reaches the provider, and cannot tell."""
    async with client_for(build(redis, provider)) as client:
        first = await client.post(CHAT_URL, json=body())
        second = await client.post(CHAT_URL, json=body())

    assert first.headers[CACHE_HEADER] == "MISS"
    assert second.headers[CACHE_HEADER] == "HIT"
    assert provider.call_count == 1
    assert second.json() == first.json()


async def test_one_token_different_misses(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """Near-identical is not identical, and this cache says so (ADR-005)."""
    async with client_for(build(redis, provider)) as client:
        first = await client.post(CHAT_URL, json=body())
        second = await client.post(
            CHAT_URL, json=body(messages=[{"role": "user", "content": "pong"}])
        )

    assert [first.headers[CACHE_HEADER], second.headers[CACHE_HEADER]] == ["MISS", "MISS"]
    assert provider.call_count == 2


async def test_a_streaming_request_bypasses_the_cache_in_both_directions(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """Streams are reported as BYPASS, reach the provider, and store nothing."""
    async with client_for(build(redis, provider)) as client:
        first = await client.post(CHAT_URL, json=body(stream=True))
        second = await client.post(CHAT_URL, json=body(stream=True))

    assert [first.headers[CACHE_HEADER], second.headers[CACHE_HEADER]] == ["BYPASS", "BYPASS"]
    assert provider.call_count == 2
    assert await entries(redis) == [], "a stream was written to the cache"


async def test_a_stream_cannot_be_served_from_a_buffered_entry(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """A stored completion must not be handed to a caller who asked for SSE.

    The digest ignores `stream`, so the entry the buffered request wrote is
    exactly the one a streaming request would find. Nothing but the bypass rule
    stops it being served as a `200 application/json` to a client parsing SSE.
    """
    async with client_for(build(redis, provider)) as client:
        await client.post(CHAT_URL, json=body())
        streamed = await client.post(CHAT_URL, json=body(stream=True))

    assert streamed.headers[CACHE_HEADER] == "BYPASS"
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert provider.call_count == 2


async def test_the_bypass_header_neither_reads_nor_writes(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """Debugging the cache must not change what is in it."""
    async with client_for(build(redis, provider)) as client:
        await client.post(CHAT_URL, json=body())
        stored = await redis.get((await entries(redis))[0])

        bypassed = await client.post(CHAT_URL, json=body(), headers=AUTH | {BYPASS_HEADER: "true"})
        after = await redis.get((await entries(redis))[0])

    assert bypassed.headers[CACHE_HEADER] == "BYPASS"
    assert provider.call_count == 2, "the bypass was served from the cache"
    assert after == stored, "the bypass overwrote the entry it was meant to skip"


async def test_an_empty_bypass_header_leaves_the_cache_in_play(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """A client that always sends the header must not disable caching by accident."""
    async with client_for(build(redis, provider)) as client:
        await client.post(CHAT_URL, json=body(), headers=AUTH | {BYPASS_HEADER: ""})
        second = await client.post(CHAT_URL, json=body(), headers=AUTH | {BYPASS_HEADER: ""})

    assert second.headers[CACHE_HEADER] == "HIT"


async def test_the_route_ttl_is_what_redis_is_told(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """The per-route entry wins over the default, and reaches the entry itself."""
    app = build(redis, provider, cache_ttl_seconds=300, cache_ttls=f"{CHAT_URL}=42")
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body())
        ttl = await redis.ttl((await entries(redis))[0])

    assert 0 < ttl <= 42


async def test_a_route_with_a_zero_ttl_is_never_cached(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """Zero is the off switch for one route, with the default still set."""
    app = build(redis, provider, cache_ttl_seconds=300, cache_ttls=f"{CHAT_URL}=0")
    async with client_for(app) as client:
        first = await client.post(CHAT_URL, json=body())
        second = await client.post(CHAT_URL, json=body())

    assert [first.headers[CACHE_HEADER], second.headers[CACHE_HEADER]] == ["BYPASS", "BYPASS"]
    assert provider.call_count == 2
    assert await entries(redis) == []


async def test_no_cache_means_no_header(provider: MockProvider) -> None:
    """A gateway with no cache says nothing about one."""
    async with client_for(build(None, provider)) as client:
        response = await client.post(CHAT_URL, json=body())

    assert CACHE_HEADER not in response.headers
    assert response.status_code == 200


async def test_a_failed_request_is_not_cached(redis: aioredis.FakeRedis) -> None:
    """An entry written for a failure would serve that failure until it expired."""
    provider = MockProvider(
        replies=[ProviderUnavailable("upstream down", provider="mock"), CannedReply("hello")]
    )
    async with client_for(build(redis, provider)) as client:
        failed = await client.post(CHAT_URL, json=body())
        recovered = await client.post(CHAT_URL, json=body())

    assert failed.status_code == 502
    assert recovered.status_code == 200
    assert recovered.json()["choices"][0]["message"]["content"] == "hello"
    assert await entries(redis) != []


# --- who shares an entry ------------------------------------------------------


async def test_entries_are_not_shared_between_keys(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """The same prompt from another key is another key's answer, and its bill."""
    app = build(redis, provider)
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body(), headers={"Authorization": "Bearer key-one"})
        second = await client.post(
            CHAT_URL, json=body(), headers={"Authorization": "Bearer key-two"}
        )

    assert second.headers[CACHE_HEADER] == "MISS"
    assert provider.call_count == 2
    assert len(await entries(redis)) == 2


async def test_the_global_scope_shares_one_namespace(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """A single-tenant deployment can opt into the hit rate it is paying for."""
    app = build(redis, provider, cache_scope="global")
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body(), headers={"Authorization": "Bearer key-one"})
        second = await client.post(
            CHAT_URL, json=body(), headers={"Authorization": "Bearer key-two"}
        )

    assert second.headers[CACHE_HEADER] == "HIT"
    assert provider.call_count == 1


def test_the_scope_is_the_only_difference_between_the_two_namespaces() -> None:
    """The digest is shared; only what precedes it is not."""
    per_key = ResponseCache(None, scope="key").key_for(
        Principal(key_id="abc"), request_for(), CHAT_URL
    )
    shared = ResponseCache(None, scope="global").key_for(
        Principal(key_id="abc"), request_for(), CHAT_URL
    )

    digest = request_digest(request_for())
    assert per_key == f"vortex:cache:abc:{CHAT_URL}:{digest}"
    assert shared == f"vortex:cache:global:{CHAT_URL}:{digest}"


# --- what a hit costs ---------------------------------------------------------


async def test_a_hit_is_recorded_as_a_request_that_bought_nothing(
    redis: aioredis.FakeRedis, tmp_path: Path, provider: MockProvider
) -> None:
    """Both halves matter: the request is counted, the tokens are not.

    Recording the cached response's usage would put tokens in the ledger that
    no invoice has, and dropping the row entirely would make `/v1/usage`
    disagree with the number of requests the caller made.
    """
    store = KeyStore(tmp_path / "keys.sqlite3")
    minted = store.create(name="cached", rpm=100, tpm=100_000)
    app = build(
        redis,
        provider,
        key_db_path=str(store.path),
        metering_enabled=True,
    )
    auth = {"Authorization": f"Bearer {minted.token}"}

    async with client_for(app) as client:
        first = await client.post(CHAT_URL, json=body(), headers=auth)
        second = await client.post(CHAT_URL, json=body(), headers=auth)

    assert second.headers[CACHE_HEADER] == "HIT"
    report = await app.state.meter.ledger.report(minted.record.key_id, days=1)
    assert report.total_requests == 2
    assert report.total_tokens == first.json()["usage"]["total_tokens"]
    assert report.total_unmetered_requests == 0


async def test_a_hit_still_spends_a_request_against_the_limit(
    redis: aioredis.FakeRedis, tmp_path: Path, provider: MockProvider
) -> None:
    """A cache that skipped the limiter would be a way around it."""
    store = KeyStore(tmp_path / "keys.sqlite3")
    minted = store.create(name="cached", rpm=2, tpm=100_000)
    app = build(redis, provider, key_db_path=str(store.path), metering_enabled=True)
    auth = {"Authorization": f"Bearer {minted.token}"}

    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body(), headers=auth)
        await client.post(CHAT_URL, json=body(), headers=auth)
        third = await client.post(CHAT_URL, json=body(), headers=auth)

    assert third.status_code == 429
    assert provider.call_count == 1, "only the first request should have been served upstream"


async def test_a_hit_returns_the_token_reservation(
    redis: aioredis.FakeRedis, tmp_path: Path, provider: MockProvider
) -> None:
    """The estimate held on admission comes back, as it does for any settlement.

    The clock is pinned because the assertion is bucket arithmetic: at
    ``tpm=10_000`` the bucket refills ~167 tokens a second, which is enough to
    hide a whole unreturned reservation on a slow runner.
    """
    store = KeyStore(tmp_path / "keys.sqlite3")
    minted = store.create(name="cached", rpm=100, tpm=10_000)
    app = build(redis, provider, key_db_path=str(store.path), metering_enabled=True)
    app.state.meter.limiter = RateLimiter(redis, clock=Clock())
    auth = {"Authorization": f"Bearer {minted.token}"}

    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body(), headers=auth)
        hit = await client.post(CHAT_URL, json=body(), headers=auth)
        # Read after the fact rather than from the response header: the header
        # reports the bucket as it was at *admission*, which is before the
        # settlement this test is about.
        level = float(await redis.hget(f"vortex:rl:{minted.record.key_id}:tokens", "tokens"))

    assert hit.headers[CACHE_HEADER] == "HIT"
    # Each request reserves ~513 tokens (512 assumed completion + a one-token
    # prompt) and gives back whatever it did not spend. Only the first request's
    # real usage should be gone; a hit that never settled would leave a second
    # reservation held, ~500 tokens below this line.
    assert level > 9_900


# --- degradation --------------------------------------------------------------


class BrokenRedis:
    """A Redis that is there and does not work, which is the interesting outage."""

    def __init__(self) -> None:
        self.calls = 0

    async def get(self, key: str) -> bytes | None:
        self.calls += 1
        raise RedisConnectionError("connection refused")

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.calls += 1
        raise RedisConnectionError("connection refused")


async def test_a_broken_cache_serves_the_request_anyway() -> None:
    """Fail open, like the limiter: a cache outage is not an outage."""
    broken = BrokenRedis()
    cache = ResponseCache(broken, default_ttl=60)
    principal = Principal(key_id="abc")

    lookup = await cache.lookup(principal, request_for(), path=CHAT_URL, headers={})
    assert lookup.outcome == "MISS"
    assert lookup.response is None

    response = ChatCompletionResponse.model_validate(
        {
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    await cache.store(lookup, response)

    assert broken.calls == 2
    assert cache.stats.errors == 2


async def test_an_entry_that_no_longer_parses_is_a_miss(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """A stored response the contract has moved past is discarded, not served."""
    async with client_for(build(redis, provider)) as client:
        await client.post(CHAT_URL, json=body())
        await redis.set((await entries(redis))[0], '{"object": "chat.completion"}')
        second = await client.post(CHAT_URL, json=body())

    assert second.status_code == 200
    assert second.headers[CACHE_HEADER] == "MISS"
    assert provider.call_count == 2


# --- the counters Day 10 reads ------------------------------------------------


async def test_the_counters_follow_what_actually_happened(
    redis: aioredis.FakeRedis, provider: MockProvider
) -> None:
    """One of each: a miss that stores, a hit, and a bypass."""
    app = build(redis, provider)
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body())
        await client.post(CHAT_URL, json=body())
        await client.post(CHAT_URL, json=body(stream=True))

    stats = app.state.cache.stats
    assert stats.snapshot() == {
        "hits": 1,
        "misses": 1,
        "bypasses": 1,
        "stores": 1,
        "errors": 0,
        "hit_rate": 0.5,
    }


def test_the_hit_rate_of_a_cache_nobody_asked_is_zero_not_an_error() -> None:
    """The first scrape happens before the first request."""
    assert ResponseCache(None).stats.hit_rate == 0.0
