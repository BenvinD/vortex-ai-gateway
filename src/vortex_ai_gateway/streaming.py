"""What a streamed completion costs, and how it ended.

A buffered completion accounts for itself: the usage object is in the body the
caller receives, and the response either happened or it did not. A stream has
neither property. The token counts arrive in a final chunk the caller may never
have asked for, and the caller may hang up half way through — by which point
the gateway has already paid for every token generated so far and has nothing
to show for it.

Both gaps are closed by the same shift: usage stops being the caller's data and
becomes the gateway's telemetry.

* :func:`metered` asks the provider for usage on *every* stream, whatever the
  caller requested. ``stream_options.include_usage`` then decides only whether
  the usage chunk is *forwarded*, which is all OpenAI compatibility actually
  requires of it (ADR-018).
* :class:`StreamRecord` accumulates the bill as chunks pass through and is
  written to the log however the stream ends — completed, failed, or abandoned
  by the client mid-generation. The abandoned case is the expensive one and the
  one an access log cannot see: it looks exactly like a short, successful
  stream (ADR-019).

A finished :class:`StreamRecord` is the whole bill for one streamed request,
which is the seam the cost accounting hangs off.
"""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

import structlog

from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    FinishReason,
    StreamOptions,
    TokenUsage,
)

logger = structlog.get_logger(__name__)

#: How a stream ended. ``abandoned`` means the client disconnected before the
#: last chunk — the tokens were generated and billed, and nobody read them.
StreamOutcome = Literal["completed", "failed", "abandoned"]

#: The one event name every streamed request settles under, whatever its
#: outcome, so a cost query is one filter rather than a union of three.
STREAM_EVENT = "stream finished"


def wants_usage(request: ChatCompletionRequest) -> bool:
    """Whether the *caller* asked to be sent the usage chunk."""
    return request.stream_options is not None and request.stream_options.include_usage


def metered(request: ChatCompletionRequest) -> ChatCompletionRequest:
    """The request as the *gateway* sends it: usage always requested.

    The provider seam keeps its documented meaning — usage is emitted when
    ``include_usage`` is set — and the gateway simply always sets it, because
    the gateway is the party being billed. What the caller asked for survives
    in the original request and is consulted by :meth:`StreamRecord.observe`
    when it decides whether to pass the usage chunk on.

    A copy, never a mutation: the request the caller sent is also what the logs
    and (later) the retry policy describe, and it should keep saying what the
    caller actually asked for.
    """
    if wants_usage(request):
        return request
    return request.model_copy(update={"stream_options": StreamOptions(include_usage=True)})


async def aclose_stream(stream: AsyncIterator[ChatCompletionChunk]) -> None:
    """Close a provider stream, if it is a generator that can be closed.

    Belt and braces for one specific case. When the cancellation that ends a
    stream is delivered while the relay is suspended at its *own* ``yield`` —
    waiting on the client's socket rather than on the provider's — the
    provider's generator is left suspended rather than unwound, and the
    upstream connection stays open until the garbage collector reaches it.
    Closing it here makes the teardown deterministic instead, which is the
    difference between paying for a few more tokens and paying for the whole
    generation.
    """
    closer = getattr(stream, "aclose", None)
    if closer is not None:
        await closer()


@dataclass
class StreamRecord:
    """The running bill for one streamed completion.

    Fed one chunk at a time by the relay, and written out once however the
    stream ends. ``usage`` is whatever the provider last reported rather than a
    count of relayed deltas: an estimate made at the edge cannot see prompt
    tokens at all, and would disagree with the invoice for the half it can see.
    """

    provider: str
    model: str
    outcome: StreamOutcome = "completed"
    chunks: int = 0
    usage: TokenUsage | None = None
    finish_reason: FinishReason | None = None
    upstream_model: str | None = None
    started: float = field(default_factory=time.perf_counter)

    def observe(
        self, chunk: ChatCompletionChunk, *, forward_usage: bool
    ) -> ChatCompletionChunk | None:
        """Account for ``chunk`` and return what the caller should be sent.

        ``None`` means the chunk existed only to carry usage the caller did not
        ask for; it is recorded and dropped. A chunk that carries usage
        *alongside* deltas — which OpenAI does not do, but nothing forbids —
        keeps its deltas and loses only the usage.
        """
        self.chunks += 1
        if chunk.vortex is not None:
            # The seam names whatever the app mounted, which for a routed
            # deployment is the router. The chunk names the adapter that
            # actually served it, under the model ID the vendor prices — and
            # those two are what an invoice is reconciled against.
            self.provider = chunk.vortex.provider
            self.upstream_model = chunk.vortex.upstream_model
        for choice in chunk.choices:
            if choice.finish_reason is not None:
                self.finish_reason = choice.finish_reason

        if chunk.usage is None:
            return chunk

        self.usage = chunk.usage
        if forward_usage:
            return chunk
        withheld = chunk.model_copy(update={"usage": None})
        return withheld if withheld.choices else None

    def log(self) -> None:
        """Write the bill.

        Anything other than a completed stream is a warning: a failure needs
        looking at, and an abandonment is spend with no delivery behind it.
        """
        usage = self.usage
        write = logger.info if self.outcome == "completed" else logger.warning
        write(
            STREAM_EVENT,
            outcome=self.outcome,
            provider=self.provider,
            model=self.model,
            upstream_model=self.upstream_model,
            chunks=self.chunks,
            finish_reason=self.finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            duration_ms=round((time.perf_counter() - self.started) * 1000, 2),
        )
