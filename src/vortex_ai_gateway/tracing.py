"""OpenTelemetry tracing: one trace per request, spans at the seams.

Spans are created by hand rather than by ``opentelemetry-instrumentation-fastapi``,
and the reason is the same one ``middleware.py`` already gives for not using
``BaseHTTPMiddleware``: the auto-instrumentation wraps the app in that shape,
which adds a task hop and does not survive contact with a streaming response
whose ending taxonomy is the most carefully built thing in this codebase
(ADR-018, ADR-019, ADR-026).

Nothing here needs to be switched off to be cheap. The OpenTelemetry *API* is
a no-op implementation until an SDK provider is installed, so
:func:`span` around a cache lookup costs a couple of attribute lookups on a
gateway that has never heard of a collector, and every module below can be
instrumented unconditionally. :func:`configure_tracing` is what installs the
real thing, and it is the only place in ``src/`` that touches the SDK.

Two things are deliberately *not* here. There is no metrics exporter — the
gateway publishes a Prometheus endpoint instead, because a scrape needs no
agent and no delivery guarantee to be useful, and the two systems answer
different questions (ADR-026). And there is no logging bridge: structlog
already writes JSON to stdout, and the correlation that was actually missing is
supplied by :func:`trace_context`, which puts the trace and span IDs into the
same context vars the request ID rides in.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

# Re-exported with the redundant alias so callers reach the whole tracing
# vocabulary through this module rather than importing OpenTelemetry in
# eight places.
from opentelemetry.trace import Span as Span
from opentelemetry.trace import StatusCode as StatusCode

from vortex_ai_gateway import __version__

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.util.types import AttributeValue

    from vortex_ai_gateway.config import Settings

    #: What a call site may hand :func:`span`. ``None`` values are dropped
    #: rather than rejected, so an optional field needs no guard at the seam.
    Attributes = Mapping[str, AttributeValue | None]

logger = structlog.get_logger(__name__)

#: The instrumentation scope every span in this codebase is created under, so
#: a collector can tell the gateway's own spans from a library's.
TRACER_NAME: Final = "vortex_ai_gateway"

#: Emitted once, when the SDK is installed. Carries the exporter and endpoint
#: because "tracing is on and no spans are arriving" is a question about those
#: two values and a firewall.
ENABLED_EVENT: Final = "tracing enabled"

#: The one place the SDK is held. Module-level because OpenTelemetry's tracer
#: provider is a process global: ``trace.set_tracer_provider`` refuses to
#: replace one that is already installed, so this is set at most once per
#: process no matter how many apps the process builds.
_provider: TracerProvider | None = None


def configure_tracing(settings: Settings, *, exporter: SpanExporter | None = None) -> bool:
    """Install a tracer provider when tracing is enabled; report whether *this call* did.

    The return value is ownership, not liveness, and follows the same rule
    ``create_app`` uses for every other resource: whoever built it closes it.
    A second call gets ``False`` and must not call :func:`shutdown_tracing`,
    because a worker that builds two apps would otherwise have the first one's
    teardown flush and stop the provider the second is still writing to.

    Idempotent and one-way. Calling it with tracing off does not uninstall an
    existing provider — OpenTelemetry's global has no supported way back — so a
    process that has traced once keeps tracing. That matters only to tests, and
    is why they inject ``exporter`` rather than reconfiguring.

    ``exporter`` overrides ``tracing_exporter`` and is attached with a
    :class:`SimpleSpanProcessor`, so a span is exported the moment it ends
    rather than on a batch timer. That is wrong for production — it exports on
    the request's own thread — and exactly right for a test that wants to
    assert on the span it just produced.
    """
    global _provider
    if not settings.tracing_enabled:
        return False

    if _provider is not None:
        if exporter is not None:
            _provider.add_span_processor(SimpleSpanProcessor(exporter))
        return False

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": __version__,
                "deployment.environment": settings.environment,
            }
        ),
        # Parent-based so a sampling decision made upstream is honoured all the
        # way down: a gateway that re-rolled the dice would produce traces with
        # holes in them, which read as "the call never happened".
        sampler=ParentBased(TraceIdRatioBased(settings.tracing_sample_ratio)),
    )
    provider.add_span_processor(_processor(settings, exporter))
    trace.set_tracer_provider(provider)
    _provider = provider

    logger.info(
        ENABLED_EVENT,
        exporter="injected" if exporter is not None else settings.tracing_exporter,
        endpoint=settings.tracing_endpoint or "otel default",
        sample_ratio=settings.tracing_sample_ratio,
        service=settings.service_name,
    )
    return True


def _processor(settings: Settings, exporter: SpanExporter | None) -> SpanProcessor:
    """The span processor this configuration asks for.

    The OTLP exporter is imported here rather than at module scope because it
    is the expensive half of the dependency — it drags in protobuf and
    ``requests`` — and a gateway with tracing off should not pay for a wire
    format it will never speak.
    """
    if exporter is not None:
        return SimpleSpanProcessor(exporter)
    if settings.tracing_exporter == "console":
        # Batched even for the console, so a slow terminal cannot add latency
        # to a request. Spans appear a second or so late; that is the trade.
        return BatchSpanProcessor(ConsoleSpanExporter())

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    endpoint = settings.tracing_endpoint.strip()
    return BatchSpanProcessor(
        OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    )


def shutdown_tracing() -> None:
    """Flush anything the batch processor is still holding.

    Called from the lifespan of the app that installed the provider, and only
    that one. Without it the last second or so of spans — which is to say,
    whatever was happening when the process was told to stop, the spans most
    worth having — dies with the worker.
    """
    if _provider is not None:
        _provider.shutdown()


def tracer() -> trace.Tracer:
    """The gateway's tracer. A no-op tracer until an SDK provider is installed."""
    return trace.get_tracer(TRACER_NAME)


@contextmanager
def span(name: str, attributes: Attributes | None = None) -> Iterator[Span]:
    """Run a block inside a child span, dropping attributes that are ``None``.

    Attributes come in as a mapping rather than as keyword arguments because
    every key here is a dotted OpenTelemetry name — ``vortex.provider``,
    ``http.route`` — and those are not Python identifiers.

    ``None`` is not a valid OpenTelemetry attribute value and the SDK logs a
    warning for each one it is handed, so call sites pass optional values
    straight through and let this drop them; the alternative is the same dict
    comprehension written out at every seam.
    """
    with tracer().start_as_current_span(
        name,
        attributes=({k: v for k, v in attributes.items() if v is not None} if attributes else None),
    ) as current:
        yield current


def trace_context() -> dict[str, str]:
    """``trace_id`` and ``span_id`` of the current span, or nothing.

    Hex, unpadded-by-OpenTelemetry's rules — 32 and 16 characters — which is
    the form a collector's UI searches by, so a line copied out of the logs can
    be pasted into Jaeger or Tempo without editing.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {
        "trace_id": trace.format_trace_id(context.trace_id),
        "span_id": trace.format_span_id(context.span_id),
    }
