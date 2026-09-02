"""Tests for the plumbing every HTTP adapter shares.

Client lifecycle, model aliasing, timeouts and the seam itself. How a failure
is *classified* is the subject of ``tests/test_error_taxonomy.py``.
"""

from typing import Any

import httpx
import pytest

from tests.upstream import Upstream, chat_request
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionChunk
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import (
    AnthropicAdapter,
    ChatProvider,
    HttpChatAdapter,
    OllamaAdapter,
    OpenAIAdapter,
)

COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "pong"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}

ADAPTERS: list[type[HttpChatAdapter]] = [OpenAIAdapter, AnthropicAdapter, OllamaAdapter]


@pytest.mark.parametrize("adapter_type", ADAPTERS, ids=lambda cls: cls.provider_name)
def test_every_adapter_satisfies_the_chat_provider_protocol(
    adapter_type: type[HttpChatAdapter],
) -> None:
    """An adapter is substitutable for the mock, and for the others."""
    assert isinstance(adapter_type(), ChatProvider)


@pytest.mark.parametrize("adapter_type", ADAPTERS, ids=lambda cls: cls.provider_name)
def test_every_adapter_has_somewhere_to_send_a_request(
    adapter_type: type[HttpChatAdapter],
) -> None:
    """Constructed bare, an adapter still knows its vendor's endpoint."""
    adapter = adapter_type()

    assert adapter.base_url.startswith("http")
    assert adapter.chat_path.startswith("/")
    assert adapter.name == adapter_type.provider_name


def test_only_the_vendors_that_charge_for_it_demand_a_key() -> None:
    """A local runtime needs no credentials, and must not be made to invent any."""
    assert OpenAIAdapter.requires_api_key
    assert AnthropicAdapter.requires_api_key
    assert not OllamaAdapter.requires_api_key


def test_connecting_is_given_a_shorter_budget_than_answering() -> None:
    """One shared timeout lets an unreachable host hold a worker for the whole
    generation budget — the failure that ``docs/notes/day-04.md`` records."""
    adapter = OpenAIAdapter(timeout=30.0, connect_timeout=2.0)

    assert adapter.client.timeout.connect == 2.0
    assert adapter.client.timeout.read == 30.0


async def test_model_alias_is_resolved_and_the_real_one_reported() -> None:
    """The caller's alias goes out translated and comes back preserved."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))
    adapter = OpenAIAdapter(client=upstream.client(), models={"fast": "gpt-4o-mini"})

    response = await adapter.complete(chat_request(model="fast"))

    assert upstream.sent["model"] == "gpt-4o-mini"
    assert response.model == "fast"
    assert response.vortex is not None
    assert response.vortex.provider == "openai"
    assert response.vortex.upstream_model == "gpt-4o-mini-2024-07-18"


async def test_an_unmapped_model_passes_through_untouched() -> None:
    """Aliasing is opt-in; an unknown name is the vendor's to reject."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))
    adapter = OpenAIAdapter(client=upstream.client(), models={"fast": "gpt-4o-mini"})

    await adapter.complete(chat_request(model="o3-pro"))

    assert upstream.sent["model"] == "o3-pro"


async def test_an_injected_client_is_not_closed_by_the_adapter() -> None:
    """The adapter closes only the pool it opened, never a shared one."""
    upstream = Upstream()
    client = upstream.client()
    adapter = OpenAIAdapter(client=client)

    await adapter.aclose()

    assert not client.is_closed
    await client.aclose()


async def test_an_owned_client_is_created_lazily_and_closed() -> None:
    """An adapter that is never called never opens a connection pool."""
    adapter = OpenAIAdapter(api_key="k")

    assert adapter._client is None
    assert adapter.client is adapter.client  # created once, then reused

    await adapter.aclose()
    assert adapter._client is None


def test_the_base_adapter_has_no_translation_of_its_own() -> None:
    """``HttpChatAdapter`` is plumbing; both ends are each vendor's to write."""
    adapter = HttpChatAdapter()

    with pytest.raises(NotImplementedError):
        adapter._encode(chat_request(), "model", stream=False)
    with pytest.raises(NotImplementedError):
        adapter._decode({}, chat_request(), "model")
    with pytest.raises(NotImplementedError):
        adapter.stream(chat_request())


def test_an_adapter_says_where_it_points_when_printed() -> None:
    """A log line or a REPL should not have to guess which endpoint this is."""
    assert "https://example.test" in repr(OpenAIAdapter(base_url="https://example.test"))


async def test_sse_framing_noise_is_ignored() -> None:
    """Comments, event names and blank separators are not payloads."""
    body = (
        ": keep-alive\n\n"
        "event: message\n"
        'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"m",'
        '"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    upstream = Upstream(httpx.Response(200, text=body))
    adapter = OpenAIAdapter(client=upstream.client())

    chunks: list[ChatCompletionChunk] = [
        chunk async for chunk in adapter.stream(chat_request(stream=True))
    ]

    assert len(chunks) == 1
    assert chunks[0].choices[0].delta.content == "hi"


async def test_an_adapter_serves_the_real_router() -> None:
    """The seam holds end to end: an adapter drops into the app unchanged."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))
    app = create_app(
        settings=Settings(_env_file=None),
        provider=OpenAIAdapter(client=upstream.client(), api_key="upstream-key"),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]},
            headers={"authorization": "Bearer client-key"},
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "pong"
    # The client's key authenticates it to the gateway; the gateway's own key
    # authenticates the gateway to the vendor. They are never the same key.
    assert upstream.headers["authorization"] == "Bearer upstream-key"
