"""Shared plumbing for adapters that talk to an HTTP provider.

This is *not* the seam. :class:`~vortex_ai_gateway.providers.base.ChatProvider`
stays a structural protocol with no base class to inherit (ADR-011); what lives
here is the part every vendor adapter would otherwise copy — building the
client, sending JSON, unwrapping server-sent events, and classifying every
failure into the taxonomy in :mod:`~vortex_ai_gateway.providers.errors`.

That classification is the load-bearing part. It happens *once*, here, so the
three adapters cannot disagree about whether a ``529`` is retryable, and so the
retry policy never has to know which vendor it is talking to.

:meth:`HttpChatAdapter.complete` is a template: encode, POST, decode. Only the
two ends differ per vendor, and they are where the interesting work is.
Streaming is left to each adapter, because the shape of a stream is exactly
what the three providers disagree about most.
"""

import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import httpx

from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    GatewayMetadata,
)
from vortex_ai_gateway.providers.errors import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

#: Matches ``Settings.request_timeout_seconds``; adapters are constructed
#: directly as well as from config, so the default lives here too.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Connecting is separate from waiting for tokens, and much faster: a TCP
#: handshake that has not completed in a few seconds is not going to. Sharing
#: one number with the read timeout means an unreachable host holds a worker
#: for the whole generation budget — see ``docs/notes/day-04.md``.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

#: Statuses that mean something more specific than their class. ``529`` is
#: Anthropic's "overloaded", which is a ``5xx`` in everything but the digit.
_STATUS_ERRORS: dict[int, type[ProviderError]] = {
    408: ProviderTimeout,
    401: ProviderAuthError,
    403: ProviderAuthError,
    429: ProviderRateLimited,
    504: ProviderTimeout,
    529: ProviderUnavailable,
}


class HttpChatAdapter:
    """Base for the vendor adapters: one HTTP client, one translation pair.

    ``models`` maps the model name a caller asks for onto the one the vendor
    knows, which is what makes ``model="fast"`` routable. The alias is what the
    caller gets back in ``response.model``; the vendor's own ID is preserved in
    ``response.vortex.upstream_model``, so provenance survives the rename.

    A caller may inject ``client`` — a shared, pooled
    :class:`httpx.AsyncClient`, or one wired to a test transport. An injected
    client is never closed by :meth:`aclose`, since this adapter does not own it.
    """

    #: Short identifier reported in ``ChatCompletionResponse.vortex``.
    provider_name: ClassVar[str] = ""
    #: Where this vendor lives when the caller does not say.
    default_base_url: ClassVar[str] = ""
    #: Path of the chat endpoint, appended to the base URL.
    chat_path: ClassVar[str] = ""
    #: Whether this vendor refuses anonymous calls. A local runtime does not,
    #: which is why this is a property of the adapter rather than an assumption
    #: made by whatever builds it.
    requires_api_key: ClassVar[bool] = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        models: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        name: str | None = None,
    ) -> None:
        self.name = name or self.provider_name
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.api_key = api_key
        self.models = dict(models or {})
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._client = client
        self._owns_client = client is None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, base_url={self.base_url!r})"

    @property
    def client(self) -> httpx.AsyncClient:
        """The HTTP client, created on first use.

        Lazily, so constructing an adapter that is never called — a routing
        table holding one per configured vendor, most of them idle — costs
        nothing and leaks no unclosed connection pool.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout)
            )
        return self._client

    async def aclose(self) -> None:
        """Release the connection pool, if this adapter opened one."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Serve ``request`` in full and return the finished completion."""
        model = self.upstream_model(request.model)
        payload = self._encode(request, model, stream=False)
        raw = await self._post(self.chat_path, payload)
        return self._decode(raw, request, model)

    def upstream_model(self, model: str) -> str:
        """Resolve the caller's model name to the one the vendor publishes."""
        return self.models.get(model, model)

    def metadata(self, upstream_model: str) -> GatewayMetadata:
        """Provenance: which adapter served this, and as which model."""
        return GatewayMetadata(provider=self.name, upstream_model=upstream_model)

    # -- Translation hooks --------------------------------------------------

    def _encode(
        self, request: ChatCompletionRequest, model: str, *, stream: bool
    ) -> dict[str, Any]:
        """Render ``request`` as the body this vendor expects."""
        raise NotImplementedError

    def _decode(
        self, payload: Any, request: ChatCompletionRequest, model: str
    ) -> ChatCompletionResponse:
        """Read this vendor's answer back into the unified contract."""
        raise NotImplementedError

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Serve ``request`` incrementally.

        Declared here so the base covers the whole ``ChatProvider`` seam and a
        half-finished adapter is a type error rather than a runtime surprise —
        but left unimplemented, because a stream's shape is the one thing no
        two vendors agree on.
        """
        raise NotImplementedError

    # -- Transport ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Headers sent with every request, authentication included."""
        return {"content-type": "application/json", "accept": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _post(self, path: str, payload: Mapping[str, Any]) -> Any:
        """POST JSON and return the parsed response body."""
        try:
            response = await self.client.post(
                self._url(path), json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise self._status_error(response.status_code, response.text, response.headers)
        return self._parse_json(response.text)

    async def _lines(self, path: str, payload: Mapping[str, Any]) -> AsyncIterator[str]:
        """Stream a response body line by line, keeping it open while iterated.

        An error status is read and raised *before* the first line is yielded,
        so a caller that fails fast never has to distinguish "the stream ended"
        from "the stream never started".
        """
        request = self.client.build_request(
            "POST", self._url(path), json=payload, headers=self._headers()
        )
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc

        try:
            if response.status_code >= httpx.codes.BAD_REQUEST:
                body = (await response.aread()).decode(errors="replace")
                raise self._status_error(response.status_code, body, response.headers)
            async for line in response.aiter_lines():
                yield line
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc
        finally:
            await response.aclose()

    @staticmethod
    async def _sse_payloads(lines: AsyncIterator[str]) -> AsyncIterator[str]:
        """Yield the ``data:`` payloads of a server-sent event stream.

        Comments, blank separators and the ``event:`` name line are dropped:
        both vendors that stream SSE here also repeat the event name inside the
        JSON, so reading it twice only creates a way for the two to disagree.
        """
        async for line in lines:
            field, _, value = line.partition(":")
            if field != "data":
                continue
            yield value.lstrip()

    # -- Failure classification ---------------------------------------------

    def _parse_json(self, text: str) -> Any:
        """Parse a body, or say which provider sent something unreadable."""
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ProviderProtocolError(
                f"{self.name} returned a body that is not JSON: {text[:200]!r}",
                provider=self.name,
            ) from exc

    def _transport_error(self, exc: httpx.HTTPError) -> ProviderError:
        """Classify a failure that happened before any status arrived.

        The distinction that matters is timeout versus everything else: a
        timeout may have left work running upstream, while a refused or reset
        connection did not. Both are retryable, and the phase that fired
        (connect, read, write, pool) is kept in the message for the log.
        """
        error_type = (
            ProviderTimeout if isinstance(exc, httpx.TimeoutException) else (ProviderUnavailable)
        )
        return error_type(
            f"{self.name} did not answer: {type(exc).__name__}: {exc}",
            provider=self.name,
            code=type(exc).__name__,
        )

    def _status_error(
        self, status_code: int, body: str, headers: Mapping[str, str]
    ) -> ProviderError:
        """Classify a non-2xx answer, quoting the vendor's own explanation."""
        message, code = _describe_error(_loads_or_none(body))
        detail = f"{self.name} returned {status_code}: {message or body[:200] or 'no detail given'}"

        if status_code == httpx.codes.TOO_MANY_REQUESTS:
            return ProviderRateLimited(
                detail,
                provider=self.name,
                status_code=status_code,
                code=code,
                retry_after=_retry_after(headers),
            )

        default = (
            ProviderUnavailable
            if status_code >= httpx.codes.INTERNAL_SERVER_ERROR
            else ProviderBadRequest
        )
        return _STATUS_ERRORS.get(status_code, default)(
            detail, provider=self.name, status_code=status_code, code=code
        )

    def _protocol_error(self, detail: str) -> ProviderProtocolError:
        return ProviderProtocolError(
            f"{self.name} returned a response this adapter cannot read: {detail}",
            provider=self.name,
        )

    def _require_mapping(self, payload: Any, what: str) -> Mapping[str, Any]:
        """Insist a decoded fragment is an object before indexing into it."""
        if not isinstance(payload, Mapping):
            raise self._protocol_error(
                f"expected {what} to be an object, got {type(payload).__name__}"
            )
        return payload


def _loads_or_none(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _describe_error(payload: Any) -> tuple[str | None, str | None]:
    """Pull ``(message, code)`` out of whichever error shape a vendor uses.

    OpenAI nests ``{"error": {"message", "type", "code"}}``, Anthropic
    ``{"error": {"type", "message"}}`` and Ollama sends ``{"error": "..."}``.
    One reader covers all three, and an unrecognised shape simply yields no
    detail rather than an exception on the failure path.
    """
    if not isinstance(payload, Mapping):
        return None, None

    error = payload.get("error", payload)
    if isinstance(error, str):
        return error, None
    if not isinstance(error, Mapping):
        return None, None

    message = error.get("message")
    code = error.get("code") or error.get("type")
    return (
        message if isinstance(message, str) else None,
        code if isinstance(code, str) else None,
    )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Read ``Retry-After``, which is either seconds or an HTTP date.

    A retry policy that ignores this and applies its own backoff gets throttled
    again, so it is worth parsing both spellings the RFC allows.
    """
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(raw)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
