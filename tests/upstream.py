"""Helpers for the adapter tests: a fake upstream, and the request it is sent.

Adapter tests are translation tests: the interesting assertions are *what was
sent* and *what came back*, not that httpx can make a request. ``Upstream``
answers from a script and keeps every request it was handed, so both halves of
a translation can be asserted from one place — with no network, no key and no
patching of the adapter's internals.

It is a respx router used as a transport rather than as a global patch, which
keeps the injected-client seam the adapters already have: the test decides
where the traffic goes, instead of respx intercepting every socket in the
process. ``tests/test_error_taxonomy.py`` uses respx the other way — as a
decorator, matched on the real vendor URLs — because there the URL is part of
what is being asserted.
"""

import json
from typing import Any

import httpx
import respx

from vortex_ai_gateway.contracts import ChatCompletionRequest


def chat_request(**overrides: Any) -> ChatCompletionRequest:
    """A minimal valid request, plus any overrides.

    Built through ``model_validate`` rather than the constructor so the tests
    exercise the same validation a real caller's JSON body does.
    """
    body: dict[str, Any] = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "ping"}],
    } | overrides
    return ChatCompletionRequest.model_validate(body)


class Upstream:
    """A scripted HTTP provider behind an :class:`httpx.MockTransport`.

    The last scripted response repeats once the script runs out, and an
    ``Exception`` in the script is raised from the transport, which is how the
    connection-failure paths are exercised.
    """

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses: list[httpx.Response | Exception] = list(responses) or [
            httpx.Response(200, json={})
        ]
        self.requests: list[httpx.Request] = []
        self.router = respx.mock(assert_all_called=False)
        self.router.route().mock(side_effect=self._handle)

    def client(self) -> httpx.AsyncClient:
        """An async client whose every request lands here instead of a socket."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.router.async_handler))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        response = self._responses[min(len(self.requests) - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def sent(self) -> dict[str, Any]:
        """The body of the most recent request, parsed."""
        body: dict[str, Any] = json.loads(self.requests[-1].content)
        return body

    @property
    def headers(self) -> httpx.Headers:
        """The headers of the most recent request."""
        return self.requests[-1].headers

    @property
    def url(self) -> str:
        """The URL of the most recent request."""
        return str(self.requests[-1].url)


def sse(*events: Any) -> httpx.Response:
    """A ``200`` whose body is those events, server-sent-event framed.

    A ``str`` event is written verbatim, which is how the ``[DONE]``
    terminator — the one payload that is not JSON — gets into a script.
    """
    body = "".join(
        f"data: {event if isinstance(event, str) else json.dumps(event)}\n\n" for event in events
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def raw_sse(body: str) -> httpx.Response:
    """A ``200`` carrying an SSE body verbatim, framing included."""
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def ndjson(*lines: Any) -> httpx.Response:
    """A ``200`` whose body is those objects, one JSON document per line."""
    body = "".join(f"{json.dumps(line)}\n" for line in lines)
    return httpx.Response(200, text=body, headers={"content-type": "application/x-ndjson"})
