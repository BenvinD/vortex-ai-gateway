"""End-to-end tests for admission and settlement through the HTTP surface.

The unit tests for the limiter and the ledger prove the pieces work. These prove
they are *wired*: that the 429 has the headers a client needs, that a success
carries its remaining allowance, that a reservation comes back when a request
fails, and that `GET /v1/usage` answers about the caller and nobody else.
"""

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fakeredis import aioredis
from fastapi.testclient import TestClient

from tests.test_spend import Clock
from tests.test_streaming import EndlessProvider, body, scope_for
from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.keys import KeyStore
from vortex_ai_gateway.metering import Meter
from vortex_ai_gateway.pricing import DEFAULT_PRICES, PriceTable
from vortex_ai_gateway.providers import MockProvider
from vortex_ai_gateway.providers.errors import ProviderUnavailable
from vortex_ai_gateway.ratelimit import RateLimiter
from vortex_ai_gateway.routes import _PENDING_SETTLEMENTS
from vortex_ai_gateway.spend import SpendLedger

CHAT_URL = "/v1/chat/completions"
USAGE_URL = "/v1/usage"
BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}


@pytest.fixture
def redis() -> aioredis.FakeRedis:
    return aioredis.FakeRedis()


@pytest.fixture
def store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.sqlite3")


def build(
    redis: aioredis.FakeRedis,
    store: KeyStore,
    provider: MockProvider | None = None,
    **overrides: object,
) -> TestClient:
    """A metered gateway, keyed from the store and backed by a fake Redis."""
    settings = Settings(
        _env_file=None,
        key_db_path=str(store.path),
        metering_enabled=True,
        **overrides,
    )
    app = create_app(settings=settings, provider=provider or MockProvider(), redis=redis)
    # The bucket refills against the clock, and these tests assert on exact
    # bucket arithmetic. At tpm=10_000 the refill is ~167 tokens a second, so
    # two requests a few milliseconds apart leave the bucket a token or two
    # above where the debits alone put it — which reads as "the refund was
    # short by one" and is invisible on a fast laptop. Pinning the clock is the
    # same fix `tests/test_spend.py` applies to the ledger, and for the same
    # reason: a loaded CI runner refills more than a developer's machine does.
    if app.state.meter.limiter is not None:
        app.state.meter.limiter = RateLimiter(redis, clock=Clock())
    return TestClient(app)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- admission ---------------------------------------------------------------


def test_a_success_carries_its_remaining_allowance(
    redis: aioredis.FakeRedis, store: KeyStore
) -> None:
    """So a client can slow down before being throttled, not after."""
    token = store.create(name="ci", rpm=10, tpm=10_000).token

    response = build(redis, store).post(CHAT_URL, json=BODY, headers=auth(token))

    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit-requests"] == "10"
    assert response.headers["x-ratelimit-remaining-requests"] == "9"
    assert response.headers["x-ratelimit-limit-tokens"] == "10000"


def test_exceeding_the_request_limit_is_a_429_with_retry_after(
    redis: aioredis.FakeRedis, store: KeyStore
) -> None:
    token = store.create(name="ci", rpm=2).token
    provider = MockProvider()
    client = build(redis, store, provider)

    outcomes = [client.post(CHAT_URL, json=BODY, headers=auth(token)) for _ in range(4)]

    assert [response.status_code for response in outcomes] == [200, 200, 429, 429]
    throttled = outcomes[-1]
    assert int(throttled.headers["retry-after"]) >= 1
    assert throttled.headers["x-ratelimit-remaining-requests"] == "0"
    assert throttled.json()["error"]["type"] == "rate_limit_error"
    # The whole point of a limit: an over-quota request costs nothing upstream.
    assert provider.call_count == 2


def test_a_gateway_limit_is_told_apart_from_a_providers(
    redis: aioredis.FakeRedis, store: KeyStore
) -> None:
    """Both are 429s, and whoever is paged needs to know which account is throttled."""
    token = store.create(rpm=1).token
    client = build(redis, store)
    client.post(CHAT_URL, json=BODY, headers=auth(token))

    throttled = client.post(CHAT_URL, json=BODY, headers=auth(token))

    assert throttled.json()["error"]["code"] == "gateway_rate_limit_exceeded"


def test_a_request_larger_than_the_whole_token_allowance_is_refused(
    redis: aioredis.FakeRedis, store: KeyStore
) -> None:
    """The token bucket rejects on its own, before the request bucket has anything to say."""
    token = store.create(rpm=100, tpm=100).token
    provider = MockProvider()
    client = build(redis, store, provider, rate_limit_assumed_completion_tokens=500)

    response = client.post(CHAT_URL, json=BODY, headers=auth(token))

    assert response.status_code == 429
    assert response.headers["x-ratelimit-remaining-tokens"] == "100"
    # And the refusal did not spend a request — the two buckets are one decision.
    assert response.headers["x-ratelimit-remaining-requests"] == "100"
    assert provider.call_count == 0


def test_limits_are_per_key(redis: aioredis.FakeRedis, store: KeyStore) -> None:
    """One noisy client must not throttle a quiet one."""
    noisy = store.create(name="noisy", rpm=1).token
    quiet = store.create(name="quiet", rpm=1).token
    client = build(redis, store)
    client.post(CHAT_URL, json=BODY, headers=auth(noisy))

    assert client.post(CHAT_URL, json=BODY, headers=auth(noisy)).status_code == 429
    assert client.post(CHAT_URL, json=BODY, headers=auth(quiet)).status_code == 200


def test_an_unlimited_key_gets_no_rate_limit_headers(
    redis: aioredis.FakeRedis, store: KeyStore
) -> None:
    """Reporting an unlimited allowance as a number is a promise we have not made."""
    token = store.create(name="internal").token

    response = build(redis, store).post(CHAT_URL, json=BODY, headers=auth(token))

    assert response.status_code == 200
    assert "x-ratelimit-limit-requests" not in response.headers


# --- settlement --------------------------------------------------------------


def remaining_tokens(response: httpx.Response) -> int:
    return int(response.headers["x-ratelimit-remaining-tokens"])


def test_a_finished_request_refunds_what_it_did_not_use(
    redis: aioredis.FakeRedis, store: KeyStore
) -> None:
    """The reservation is an estimate; the reconcile is what makes the limit mean tokens.

    Each request reserves ~1001 tokens and the mock spends 5. Between the two
    responses the bucket should therefore have moved by 5, not by 1001: what a
    caller is charged is what they used, and the reservation only governs how
    much they may have in flight.
    """
    token = store.create(tpm=10_000).token
    client = build(redis, store, rate_limit_assumed_completion_tokens=1_000)

    first = client.post(CHAT_URL, json=BODY, headers=auth(token))
    second = client.post(CHAT_URL, json=BODY, headers=auth(token))

    spent = client.get(USAGE_URL, headers=auth(token)).json()["total_tokens"]
    assert remaining_tokens(first) - remaining_tokens(second) == spent // 2


def test_a_failed_request_gets_its_whole_reservation_back(
    redis: aioredis.FakeRedis, store: KeyStore
) -> None:
    """An outage must not burn a caller's allowance on calls that produced nothing."""
    token = store.create(tpm=10_000).token
    provider = MockProvider(replies=[ProviderUnavailable("down", provider="mock")])
    client = build(redis, store, provider, rate_limit_assumed_completion_tokens=1_000)

    failed = client.post(CHAT_URL, json=BODY, headers=auth(token))
    assert failed.status_code == 502

    # A rate-limit header on the failure, and a second request that finds the
    # bucket exactly where the first one did: the outage cost nothing.
    assert failed.headers["x-ratelimit-limit-tokens"] == "10000"
    again = client.post(CHAT_URL, json=BODY, headers=auth(token))
    assert remaining_tokens(again) == remaining_tokens(failed)
    # And nothing was written to the ledger: a failure is not usage.
    assert client.get(USAGE_URL, headers=auth(token)).json()["total_requests"] == 0


def test_a_streamed_request_is_metered_and_settled(
    redis: aioredis.FakeRedis, store: KeyStore
) -> None:
    """Headers commit with the status, so they must be set before the first token."""
    token = store.create(rpm=10, tpm=10_000).token
    client = build(redis, store, rate_limit_assumed_completion_tokens=1_000)

    with client.stream(
        "POST", CHAT_URL, json={**BODY, "stream": True}, headers=auth(token)
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-ratelimit-remaining-requests"] == "9"
        body = "".join(response.iter_text())

    assert body.endswith("data: [DONE]\n\n")
    report = client.get(USAGE_URL, headers=auth(token)).json()
    assert report["total_requests"] == 1
    assert report["total_tokens"] > 0


# --- the ledger, through the endpoint ----------------------------------------


def test_usage_reports_what_the_caller_spent(redis: aioredis.FakeRedis, store: KeyStore) -> None:
    token = store.create(name="ci").token
    client = build(redis, store)
    for _ in range(3):
        client.post(CHAT_URL, json=BODY, headers=auth(token))

    report = client.get(USAGE_URL, headers=auth(token)).json()

    assert report["object"] == "usage.report"
    assert report["total_requests"] == 3
    assert report["daily"][-1]["models"][0]["model"] == "gpt-4o-mini"


def test_usage_only_ever_reports_your_own(redis: aioredis.FakeRedis, store: KeyStore) -> None:
    """There is no key parameter, deliberately: naming another key needs an
    authorisation system this gateway does not have."""
    mine = store.create(name="mine").token
    theirs = store.create(name="theirs").token
    client = build(redis, store)
    for _ in range(5):
        client.post(CHAT_URL, json=BODY, headers=auth(theirs))

    assert client.get(USAGE_URL, headers=auth(mine)).json()["total_requests"] == 0
    assert client.get(USAGE_URL, headers=auth(theirs)).json()["total_requests"] == 5


def test_usage_costs_are_strings_not_floats(
    redis: aioredis.FakeRedis, store: KeyStore, tmp_path: Path
) -> None:
    """A caller reconciling against an invoice must get back what we computed."""
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"gpt-4o-mini": {"prompt": 1.0, "completion": 1.0}}))
    token = store.create(name="ci").token
    client = build(redis, store, price_table_path=str(prices))
    client.post(CHAT_URL, json=BODY, headers=auth(token))

    raw = client.get(USAGE_URL, headers=auth(token)).text

    assert '"total_cost_usd":"0.' in raw.replace(" ", "")
    assert Decimal(json.loads(raw)["total_cost_usd"]) > 0


def test_usage_requires_a_key(redis: aioredis.FakeRedis, store: KeyStore) -> None:
    """It is on the /v1 router, so it inherits the router-level dependency."""
    assert build(redis, store).get(USAGE_URL).status_code == 401


def test_usage_rejects_an_out_of_range_window(redis: aioredis.FakeRedis, store: KeyStore) -> None:
    token = store.create().token

    response = build(redis, store).get(f"{USAGE_URL}?days=0", headers=auth(token))

    assert response.status_code == 400


# --- a gateway with no Redis at all ------------------------------------------


@pytest.fixture
def unmetered() -> Iterator[TestClient]:
    """The local-development default: no metering, no Redis, no key database."""
    with TestClient(
        create_app(settings=Settings(_env_file=None), provider=MockProvider())
    ) as client:
        yield client


def test_an_unmetered_gateway_serves_without_headers(unmetered: TestClient) -> None:
    response = unmetered.post(CHAT_URL, json=BODY, headers=auth("anything"))

    assert response.status_code == 200
    assert "x-ratelimit-limit-requests" not in response.headers


def test_an_unmetered_gateway_still_answers_usage(unmetered: TestClient) -> None:
    """The endpoint exists either way, so a client can be written once."""
    response = unmetered.get(USAGE_URL, headers=auth("anything"))

    assert response.status_code == 200
    assert response.json()["total_requests"] == 0


def test_the_factory_opens_and_closes_its_own_redis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection is lazy, so this never touches the network — but the
    factory must still build one, hand it to the meter, and close it on
    shutdown, or a restarted worker leaks its pool."""
    settings = Settings(
        _env_file=None,
        metering_enabled=True,
        redis_url="redis://localhost:6379/15",
        key_db_path=str(tmp_path / "keys.sqlite3"),
    )
    app = create_app(settings=settings, provider=MockProvider())

    assert app.state.redis is not None
    assert app.state.meter.limiter is not None
    assert app.state.meter.ledger is not None

    closed: list[bool] = []
    monkeypatch.setattr(app.state.redis, "aclose", lambda: _note(closed))

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    # Closed by the lifespan, not left to the garbage collector.
    assert closed == [True]


async def _note(closed: list[bool]) -> None:
    closed.append(True)


async def test_a_request_whose_usage_never_arrived_is_settled_as_unmetered() -> None:
    """The abandoned-stream case, at the seam rather than through a socket.

    Its reservation is *not* refunded: the estimate is the only evidence there
    is that it cost anything, and the alternative — handing the allowance back —
    would make abandoning streams the cheapest way to use the gateway.
    """
    redis = aioredis.FakeRedis()
    settings = Settings(_env_file=None, rate_limit_assumed_completion_tokens=1_000)
    limiter = RateLimiter(redis)
    ledger = SpendLedger(redis, PriceTable(DEFAULT_PRICES))
    meter = Meter(settings, limiter=limiter, ledger=ledger)
    principal = Principal(key_id="k-1", tpm=10_000)
    request = ChatCompletionRequest.model_validate(BODY)

    reservation = await meter.admit(principal, request)
    await meter.settle(reservation, None)

    report = await ledger.report("k-1", days=1)
    assert report.total_requests == 1
    assert report.total_unmetered_requests == 1
    assert report.total_tokens == 0
    # The hold stays held: 10000 less the 1001 reserved, and nothing given back.
    assert (await limiter.admit(principal, tokens=1)).tokens.remaining == 8_998


# --- the abandoned stream, metered -------------------------------------------


async def test_an_abandoned_stream_still_settles(tmp_path: Path) -> None:
    """The case that awaiting the settlement in place silently loses.

    An abandoned stream reaches the accounting code inside a scope Starlette has
    already cancelled. Under that scope the next `await` that yields raises
    `CancelledError` immediately — so with `meter.settle` awaited in place, the
    Redis write never completed, nothing was logged about it, and the request
    that most needs a record (tokens generated, none counted, reservation still
    held) was the one the ledger never heard about.

    Driven as raw ASGI rather than through `TestClient`, because a client
    hanging up is an `http.disconnect` message and the only way to send one
    after a specific chunk is to be the server.
    """
    redis = _suspending(aioredis.FakeRedis())
    settings = Settings(
        _env_file=None,
        key_db_path=str(KeyStore(tmp_path / "keys.sqlite3").path),
        metering_enabled=True,
    )
    store = KeyStore(tmp_path / "keys.sqlite3")
    minted = store.create(name="streamer", tpm=100_000)
    provider = EndlessProvider()
    app = create_app(settings=settings, provider=provider, redis=redis)

    await _hang_up_after(app, minted.token, chunks=3)
    await _drain_settlements()

    assert provider.closed, "the provider stream was left open, still generating"
    report = await app.state.meter.ledger.report(minted.record.key_id, days=1)
    assert report.total_requests == 1
    assert report.total_unmetered_requests == 1, "an abandoned stream is not a free one"


def _suspending(redis: aioredis.FakeRedis) -> aioredis.FakeRedis:
    """A fake Redis whose writes yield, as a real one's network I/O does.

    Without this the fake completes without ever suspending, and the bug under
    test — a cancelled scope killing the first `await` that yields — cannot
    happen at all.
    """
    build = redis.pipeline

    def pipeline(*args: object, **kwargs: object) -> object:
        pipe = build(*args, **kwargs)
        execute = pipe.execute

        async def suspending_execute() -> object:
            await asyncio.sleep(0)
            return await execute()

        pipe.execute = suspending_execute
        return pipe

    redis.pipeline = pipeline  # type: ignore[method-assign]
    return redis


async def _hang_up_after(app: object, token: str, *, chunks: int) -> None:
    """Stream from ``app`` as a server would, then disconnect after ``chunks``."""
    payload = json.dumps(body()).encode()
    scope = scope_for(payload)
    scope["headers"] = [header for header in scope["headers"] if header[0] != b"authorization"] + [
        (b"authorization", f"Bearer {token}".encode())
    ]

    delivered: list[bytes] = []
    hung_up = asyncio.Event()
    inbound: list[dict[str, object]] = [
        {"type": "http.request", "body": payload, "more_body": False}
    ]

    async def receive() -> dict[str, object]:
        if inbound:
            return inbound.pop(0)
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            delivered.append(message.get("body", b""))  # type: ignore[arg-type]
            if len(delivered) >= chunks:
                hung_up.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=5)  # type: ignore[operator]


async def _drain_settlements() -> None:
    """Wait for the settlements the abandoned path spawned rather than awaited."""
    while _PENDING_SETTLEMENTS:
        await asyncio.gather(*_PENDING_SETTLEMENTS)
