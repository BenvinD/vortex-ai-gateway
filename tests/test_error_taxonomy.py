"""Every way a provider can fail, and the error type it becomes.

These are the tests the retry policy will be built on, so they assert the
classification *and* :attr:`~vortex_ai_gateway.providers.errors.ProviderError.retryable`
— a status silently reclassified from retryable to not is exactly the change
that turns a transient outage into a stuck queue, or a permanent failure into a
retry storm.

They use respx as it is meant to be used: routes matched on the vendors' real
URLs, with the adapter building its own client. That makes the endpoint path
part of the assertion — an adapter posting to the wrong path would otherwise
pass every translation test in the suite.
"""

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx

from tests.upstream import Upstream, chat_request
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import (
    AnthropicAdapter,
    HttpChatAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    TranslationError,
    UnsupportedParameterError,
)

AdapterFactory = Callable[[], HttpChatAdapter]

#: Each adapter as it is really configured, and the URL it must post to.
ADAPTERS = [
    pytest.param(
        lambda: OpenAIAdapter(api_key="sk-test"),
        "https://api.openai.com/v1/chat/completions",
        id="openai",
    ),
    pytest.param(
        lambda: AnthropicAdapter(api_key="sk-ant-test"),
        "https://api.anthropic.com/v1/messages",
        id="anthropic",
    ),
    pytest.param(OllamaAdapter, "http://localhost:11434/api/chat", id="ollama"),
]

#: Upstream status, the error it becomes, and whether trying again is sensible.
STATUS_CASES = [
    (400, ProviderBadRequest, False),
    (401, ProviderAuthError, False),
    (403, ProviderAuthError, False),
    (404, ProviderBadRequest, False),
    (408, ProviderTimeout, True),
    (409, ProviderBadRequest, False),
    (422, ProviderBadRequest, False),
    (429, ProviderRateLimited, True),
    (500, ProviderUnavailable, True),
    (502, ProviderUnavailable, True),
    (503, ProviderUnavailable, True),
    (504, ProviderTimeout, True),
    # Anthropic's "overloaded": a 5xx in everything but the leading digit.
    (529, ProviderUnavailable, True),
]

#: What httpx raises before any status arrives, and what it becomes.
TRANSPORT_CASES = [
    (httpx.ConnectTimeout("handshake timed out"), ProviderTimeout, True),
    (httpx.ReadTimeout("no tokens in time"), ProviderTimeout, True),
    (httpx.WriteTimeout("could not send"), ProviderTimeout, True),
    (httpx.PoolTimeout("no free connection"), ProviderTimeout, True),
    (httpx.ConnectError("connection refused"), ProviderUnavailable, True),
    (httpx.ReadError("connection reset"), ProviderUnavailable, True),
    (httpx.RemoteProtocolError("malformed HTTP"), ProviderUnavailable, True),
]


@contextmanager
def upstream_at(url: str, outcome: httpx.Response | Exception) -> Iterator[respx.Route]:
    """Answer ``url`` with ``outcome``, and nothing else with anything."""
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url)
        if isinstance(outcome, httpx.Response):
            route.mock(return_value=outcome)
        else:
            route.mock(side_effect=outcome)
        yield route


# -- Status classification --------------------------------------------------


@pytest.mark.parametrize(("build", "url"), ADAPTERS)
@pytest.mark.parametrize(
    ("status", "expected", "retryable"), STATUS_CASES, ids=lambda case: str(case)
)
async def test_a_status_becomes_the_error_a_retry_can_branch_on(
    build: AdapterFactory,
    url: str,
    status: int,
    expected: type[ProviderError],
    retryable: bool,
) -> None:
    with upstream_at(url, httpx.Response(status, json={"error": {"message": "nope"}})) as route:
        adapter = build()
        with pytest.raises(expected) as caught:
            await adapter.complete(chat_request())

    assert route.called, f"the adapter did not post to {url}"
    assert caught.value.status_code == status
    assert caught.value.retryable is retryable
    assert caught.value.provider == adapter.name


@pytest.mark.parametrize(("build", "url"), ADAPTERS)
@pytest.mark.parametrize(
    ("failure", "expected", "retryable"),
    TRANSPORT_CASES,
    ids=lambda case: type(case).__name__ if isinstance(case, Exception) else str(case),
)
async def test_a_connection_failure_becomes_the_error_a_retry_can_branch_on(
    build: AdapterFactory,
    url: str,
    failure: Exception,
    expected: type[ProviderError],
    retryable: bool,
) -> None:
    """No status ever arrived, so the httpx exception is all there is to go on."""
    with upstream_at(url, failure):
        adapter = build()
        with pytest.raises(expected) as caught:
            await adapter.complete(chat_request())

    assert caught.value.retryable is retryable
    assert caught.value.status_code is None
    assert caught.value.code == type(failure).__name__


@pytest.mark.parametrize(("build", "url"), ADAPTERS)
async def test_a_failing_stream_is_classified_before_the_first_chunk(
    build: AdapterFactory, url: str
) -> None:
    """A caller that fails fast never has to tell "ended" from "never started"."""
    with upstream_at(url, httpx.Response(503, json={"error": {"message": "down"}})):
        adapter = build()
        with pytest.raises(ProviderUnavailable):
            async for _ in adapter.stream(chat_request(stream=True)):
                pytest.fail("a chunk was yielded before the error surfaced")


@pytest.mark.parametrize(("build", "url"), ADAPTERS)
async def test_a_body_that_is_not_json_is_a_protocol_error(build: AdapterFactory, url: str) -> None:
    """A 200 carrying a proxy's HTML error page is not a completion."""
    with upstream_at(url, httpx.Response(200, text="<html>hello from a proxy</html>")):
        adapter = build()
        with pytest.raises(ProviderProtocolError) as caught:
            await adapter.complete(chat_request())

    assert caught.value.retryable is False, "the same request reproduces it exactly"


# -- What the vendor said ---------------------------------------------------


@pytest.mark.parametrize(
    ("build", "url", "body", "message", "code"),
    [
        pytest.param(
            lambda: OpenAIAdapter(api_key="sk-test"),
            "https://api.openai.com/v1/chat/completions",
            {
                "error": {
                    "message": "This model does not exist",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
            "This model does not exist",
            "model_not_found",
            id="openai",
        ),
        pytest.param(
            lambda: AnthropicAdapter(api_key="sk-ant-test"),
            "https://api.anthropic.com/v1/messages",
            {"type": "error", "error": {"type": "not_found_error", "message": "model not found"}},
            "model not found",
            "not_found_error",
            id="anthropic",
        ),
        pytest.param(
            OllamaAdapter,
            "http://localhost:11434/api/chat",
            {"error": 'model "llama9" not found, try pulling it first'},
            'model "llama9" not found',
            None,
            id="ollama",
        ),
    ],
)
async def test_the_vendors_own_explanation_is_carried_through(
    build: AdapterFactory, url: str, body: dict[str, Any], message: str, code: str | None
) -> None:
    """Three vendors, three error envelopes, one reader."""
    with upstream_at(url, httpx.Response(404, json=body)):
        adapter = build()
        with pytest.raises(ProviderBadRequest) as caught:
            await adapter.complete(chat_request())

    assert message in str(caught.value)
    assert caught.value.code == code


async def test_an_unreadable_error_body_still_reports_the_status() -> None:
    """The failure path must never be the thing that raises."""
    url = "https://api.openai.com/v1/chat/completions"
    with upstream_at(url, httpx.Response(502, text="<html>bad gateway</html>")):
        with pytest.raises(ProviderUnavailable, match="502") as caught:
            await OpenAIAdapter(api_key="k").complete(chat_request())

    assert caught.value.code is None


# -- Rate limiting ----------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({"retry-after": "30"}, 30.0),
        ({"retry-after": "0"}, 0.0),
        ({}, None),
        ({"retry-after": "whenever you like"}, None),
    ],
    ids=["seconds", "zero", "absent", "unparseable"],
)
async def test_the_wait_the_provider_asked_for_is_kept(
    header: dict[str, str], expected: float | None
) -> None:
    """A retry that ignores ``Retry-After`` is throttled again immediately."""
    url = "https://api.openai.com/v1/chat/completions"
    with upstream_at(
        url, httpx.Response(429, json={"error": {"message": "slow down"}}, headers=header)
    ):
        with pytest.raises(ProviderRateLimited) as caught:
            await OpenAIAdapter(api_key="k").complete(chat_request())

    assert caught.value.retry_after == expected


async def test_a_retry_after_date_is_read_as_a_delay() -> None:
    """The RFC allows an HTTP date, and some proxies send one."""
    url = "https://api.openai.com/v1/chat/completions"
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)

    with upstream_at(url, httpx.Response(429, headers={"retry-after": when})):
        with pytest.raises(ProviderRateLimited) as caught:
            await OpenAIAdapter(api_key="k").complete(chat_request())

    assert caught.value.retry_after is not None
    assert 50 <= caught.value.retry_after <= 61


# -- Failures we raise before sending anything ------------------------------


def test_a_request_we_cannot_send_is_a_bad_request_and_never_retried() -> None:
    """A translation failure is the caller's, and no retry will fix it."""
    assert issubclass(TranslationError, ProviderBadRequest)
    assert issubclass(UnsupportedParameterError, TranslationError)
    assert TranslationError.retryable is False


# -- What the caller sees ---------------------------------------------------


@pytest.mark.parametrize(
    ("upstream_status", "gateway_status"),
    [
        (400, 400),
        # *Our* key was refused, not the caller's: reporting 401 would send
        # them to rotate a key that is perfectly good.
        (401, 502),
        (404, 400),
        (429, 429),
        (500, 502),
        (503, 502),
        (504, 504),
    ],
)
async def test_the_taxonomy_decides_the_status_the_caller_gets(
    upstream_status: int, gateway_status: int
) -> None:
    upstream = Upstream(httpx.Response(upstream_status, json={"error": {"message": "nope"}}))
    response = await _post_through_gateway(OpenAIAdapter(client=upstream.client()))

    assert response.status_code == gateway_status
    assert response.json()["error"]["message"]


async def test_a_throttled_caller_is_told_how_long_to_wait() -> None:
    """The vendor's backoff is forwarded rather than reinvented."""
    upstream = Upstream(
        httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "12"})
    )
    response = await _post_through_gateway(OpenAIAdapter(client=upstream.client()))

    assert response.status_code == 429
    assert response.headers["retry-after"] == "12"
    assert response.json()["error"]["type"] == "rate_limit_error"


async def test_an_unsupported_parameter_names_itself_in_the_envelope() -> None:
    """A 400 the caller can act on, rather than a 502 they cannot."""
    upstream = Upstream(httpx.Response(200, json={}))
    response = await _post_through_gateway(
        AnthropicAdapter(client=upstream.client(), api_key="k"),
        body={
            "model": "claude-sonnet-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 4,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "n"
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert not upstream.requests, "the request must not reach the vendor"


async def _post_through_gateway(
    provider: HttpChatAdapter, body: dict[str, Any] | None = None
) -> httpx.Response:
    """Drive one chat request through the whole app over ASGI."""
    app = create_app(settings=Settings(_env_file=None), provider=provider)
    transport = httpx.ASGITransport(app=app)
    payload = body or {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}

    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        return await client.post(
            "/v1/chat/completions", json=payload, headers={"authorization": "Bearer client-key"}
        )


# -- Failures with no status to read ----------------------------------------


async def test_a_stream_that_cannot_connect_is_classified_too() -> None:
    """The failure happens before there is a response to read a status from."""
    url = "https://api.openai.com/v1/chat/completions"
    with upstream_at(url, httpx.ConnectError("connection refused")):
        adapter = OpenAIAdapter(api_key="k")
        with pytest.raises(ProviderUnavailable, match="ConnectError"):
            async for _ in adapter.stream(chat_request(stream=True)):
                pass


async def test_a_connection_lost_mid_stream_is_classified_too() -> None:
    """Half an answer is a failure, and a retryable one."""

    async def collapse() -> AsyncIterator[bytes]:
        yield b'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"m",'
        yield b'"choices":[]}\n\n'
        raise httpx.ReadError("connection reset")

    upstream = Upstream(httpx.Response(200, content=collapse()))
    adapter = OpenAIAdapter(client=upstream.client())

    with pytest.raises(ProviderUnavailable, match="ReadError") as caught:
        async for _ in adapter.stream(chat_request(stream=True)):
            pass

    assert caught.value.retryable is True


async def test_a_completion_that_is_not_an_object_is_a_protocol_error() -> None:
    """A JSON array parses cleanly and is still not a completion."""
    upstream = Upstream(httpx.Response(200, json=["not", "a", "completion"]))

    with pytest.raises(ProviderProtocolError, match="expected the completion to be an object"):
        await OpenAIAdapter(client=upstream.client()).complete(chat_request())


async def test_an_error_body_of_an_unexpected_shape_is_survivable() -> None:
    """``error`` is usually an object or a string; here it is neither."""
    upstream = Upstream(httpx.Response(503, json={"error": ["down", "for", "maintenance"]}))

    with pytest.raises(ProviderUnavailable, match="503") as caught:
        await OpenAIAdapter(client=upstream.client()).complete(chat_request())

    assert caught.value.code is None


async def test_a_retry_after_date_without_a_timezone_is_still_read() -> None:
    """``parsedate_to_datetime`` returns a naive datetime for a date with no zone."""
    upstream = Upstream(httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00"}))

    with pytest.raises(ProviderRateLimited) as caught:
        await OpenAIAdapter(client=upstream.client()).complete(chat_request())

    assert caught.value.retry_after == 0.0, "a date in the past means 'now'"
