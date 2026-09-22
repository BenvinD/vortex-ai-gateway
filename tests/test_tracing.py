"""One trace per request, and the seams that hang off it.

These tests install a real SDK tracer provider with an in-memory exporter and
then drive the app over HTTP, because everything worth asserting here is about
*shape* — which spans exist, which is whose parent, what they are named — and a
unit test of :func:`span` would only prove OpenTelemetry works.

The provider is installed once for the session on purpose. OpenTelemetry's
tracer provider is a process global that refuses to be replaced, which is the
same reason :func:`configure_tracing` is documented as one-way.
"""

from collections.abc import Iterator

import pytest
import structlog
from fakeredis import aioredis
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from tests.test_cache import body, client_for
from tests.test_semantic import ScriptedEmbedder
from tests.test_streaming import records
from vortex_ai_gateway.cache import BYPASS_HEADER
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import CannedReply, MockProvider
from vortex_ai_gateway.providers.errors import ProviderUnavailable
from vortex_ai_gateway.providers.resilience_wrapper import ResilientProvider
from vortex_ai_gateway.resilience import CircuitBreaker, RetryPolicy
from vortex_ai_gateway.tracing import (
    _processor,
    configure_tracing,
    shutdown_tracing,
    span,
    trace_context,
)

CHAT_URL = "/v1/chat/completions"


@pytest.fixture(scope="session")
def _exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    configure_tracing(Settings(_env_file=None, tracing_enabled=True), exporter=exporter)
    return exporter


@pytest.fixture
def spans(_exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    _exporter.clear()
    yield _exporter
    _exporter.clear()


def build(provider: object | None = None, **overrides: object) -> object:
    settings = Settings(_env_file=None, tracing_enabled=True, **overrides)
    return create_app(settings=settings, provider=provider or MockProvider())


def named(spans: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.name == name]


def only(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    (found,) = named(spans, name)
    return found


# --- configuration ---------------------------------------------------------


def test_tracing_off_installs_nothing_and_claims_nothing() -> None:
    """And the caller must not then try to shut a provider down."""
    assert configure_tracing(Settings(_env_file=None)) is False


def test_only_the_first_caller_owns_the_provider(spans: InMemorySpanExporter) -> None:
    """Ownership, so a second app in one worker cannot flush the first's spans."""
    assert configure_tracing(Settings(_env_file=None, tracing_enabled=True)) is False


def test_a_span_drops_attributes_that_were_not_set(spans: InMemorySpanExporter) -> None:
    with span("probe", {"kept": "yes", "dropped": None}):
        pass

    assert dict(only(spans, "probe").attributes or {}) == {"kept": "yes"}


def test_there_is_no_trace_context_outside_a_span() -> None:
    assert trace_context() == {}


# --- the server span -------------------------------------------------------


async def test_one_server_span_per_request_named_for_the_route(
    spans: InMemorySpanExporter,
) -> None:
    """The route template, not the path: a span name per URL is not a name."""
    async with client_for(build()) as client:
        await client.post(CHAT_URL, json=body())

    server = only(spans, f"POST {CHAT_URL}")
    attributes = dict(server.attributes or {})
    assert attributes["http.route"] == CHAT_URL
    assert attributes["http.response.status_code"] == 200
    assert attributes["vortex.model"] == "gpt-4o-mini"
    assert attributes["vortex.provider"] == "mock"
    assert attributes["vortex.request_id"]


async def test_the_cache_and_provider_spans_are_children_of_the_request(
    spans: InMemorySpanExporter,
) -> None:
    """The shape the whole exercise is for: one trace, branching at the seams."""
    async with client_for(build()) as client:
        await client.post(CHAT_URL, json=body())

    server = only(spans, f"POST {CHAT_URL}")
    children = {s.name for s in spans.get_finished_spans() if s.parent is not None}
    assert {"cache.exact.lookup", "provider.complete"} <= children
    for name in ("cache.exact.lookup", "provider.complete"):
        child = only(spans, name)
        assert child.parent is not None
        assert child.parent.span_id == server.context.span_id
        assert child.context.trace_id == server.context.trace_id


async def test_a_bypassed_request_gets_no_semantic_span_either(
    spans: InMemorySpanExporter,
) -> None:
    """A bypass is about the request, not one tier, so neither tier is asked.

    The trace is where that claim is checkable: an operator who sent the bypass
    header to get an answer the cache cannot have touched would otherwise have
    no way to see that the *second* tier was quietly consulted anyway
    (ADR-004, ADR-024).
    """
    app = create_app(
        settings=Settings(_env_file=None, tracing_enabled=True, cache_enabled=True),
        provider=MockProvider(),
        redis=aioredis.FakeRedis(),
    )
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body(), headers={BYPASS_HEADER: "true"})

    assert dict(only(spans, "cache.exact.lookup").attributes or {})["vortex.cache.outcome"] == (
        "BYPASS"
    )
    assert named(spans, "cache.semantic.lookup") == []


async def test_a_failed_request_marks_the_server_span_as_an_error(
    spans: InMemorySpanExporter,
) -> None:
    """A handled 502 returns normally, so nothing else would mark the span."""
    provider = MockProvider(replies=(ProviderUnavailable("down", provider="mock"),))
    async with client_for(build(provider)) as client:
        response = await client.post(CHAT_URL, json=body())

    assert response.status_code == 502
    assert only(spans, f"POST {CHAT_URL}").status.status_code is StatusCode.ERROR


# --- the retry storm -------------------------------------------------------


async def test_every_retry_attempt_is_its_own_span(
    spans: InMemorySpanExporter, sleeps: list[float]
) -> None:
    """Three attempt spans under one provider span: the picture Day 5 lacked."""
    flaky = MockProvider(
        replies=(
            ProviderUnavailable("down", provider="mock"),
            ProviderUnavailable("down", provider="mock"),
            CannedReply("at last"),
        )
    )
    resilient = ResilientProvider(
        inner=flaky,
        policy=RetryPolicy(max_attempts=3, backoff_base=1.0, max_backoff=8.0),
        breaker=CircuitBreaker(name="mock", failure_threshold=5),
    )

    async with client_for(build(resilient)) as client:
        response = await client.post(CHAT_URL, json=body())

    assert response.status_code == 200
    attempts = named(spans, "provider.attempt")
    assert [dict(s.attributes or {})["vortex.attempt"] for s in attempts] == [1, 2, 3]
    # The first two recorded the failure that caused the next attempt; the
    # third did not, and that is how a trace shows a retry that *worked*.
    assert [s.status.status_code is StatusCode.ERROR for s in attempts] == [True, True, False]
    parent = only(spans, "provider.complete")
    assert {s.parent.span_id for s in attempts if s.parent} == {parent.context.span_id}


# --- correlation with the logs ---------------------------------------------


async def test_the_access_log_carries_the_trace_id(
    spans: InMemorySpanExporter, capsys: pytest.CaptureFixture[str]
) -> None:
    """The half of the correlation that was actually missing (ADR-026).

    A trace ID in the logs is what turns "this request was slow" into the
    waterfall that says which part of it was, without a timestamp search.
    """
    async with client_for(build()) as client:
        await client.post(CHAT_URL, json=body())
    structlog.contextvars.clear_contextvars()

    (completed,) = records(capsys.readouterr().out, "request completed")
    server = only(spans, f"POST {CHAT_URL}")
    assert completed["trace_id"] == f"{server.context.trace_id:032x}"
    assert completed["request_id"]


# --- the exporter the SDK is pointed at ------------------------------------


def test_the_console_exporter_is_still_batched() -> None:
    """A slow terminal must not become latency on a request."""
    processor = _processor(Settings(_env_file=None, tracing_exporter="console"), None)

    assert isinstance(processor, BatchSpanProcessor)
    assert isinstance(processor.span_exporter, ConsoleSpanExporter)


def test_an_explicit_collector_endpoint_is_used_verbatim() -> None:
    settings = Settings(
        _env_file=None,
        tracing_exporter="otlp",
        tracing_endpoint="http://collector.internal:4318/v1/traces",
    )

    processor = _processor(settings, None)

    assert processor.span_exporter._endpoint == "http://collector.internal:4318/v1/traces"


def test_no_endpoint_defers_to_the_sdks_own_environment_variables() -> None:
    """So a host already configured for OpenTelemetry needs nothing set here."""
    processor = _processor(Settings(_env_file=None, tracing_exporter="otlp"), None)

    assert processor.span_exporter._endpoint.endswith("/v1/traces")


def test_an_injected_exporter_is_attached_to_a_provider_that_already_exists(
    spans: InMemorySpanExporter,
) -> None:
    """How a second test file would watch spans without a second provider."""
    second = InMemorySpanExporter()
    configure_tracing(Settings(_env_file=None, tracing_enabled=True), exporter=second)

    with span("probe"):
        pass

    assert [s.name for s in second.get_finished_spans()] == ["probe"]
    assert named(spans, "probe"), "the first exporter still sees it too"


# --- shutdown --------------------------------------------------------------


class RecordingProcessor(SpanProcessor):
    """Records that it was shut down, which is what a flush travels through."""

    def __init__(self) -> None:
        self.shut_down = False

    def shutdown(self) -> None:
        self.shut_down = True


def test_shutdown_reaches_the_processors_holding_the_last_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the batch that was in flight when the worker stopped is lost.

    Driven against a throwaway provider rather than the session's, because
    shutting the real one down would take the rest of this file's spans with
    it — which is the same hazard `configure_tracing` returns ownership to
    prevent in a worker that builds two apps.
    """
    provider = TracerProvider()
    recorder = RecordingProcessor()
    provider.add_span_processor(recorder)
    monkeypatch.setattr("vortex_ai_gateway.tracing._provider", provider)

    shutdown_tracing()

    assert recorder.shut_down is True


def test_shutdown_with_no_provider_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway with tracing off still runs the same lifespan."""
    monkeypatch.setattr("vortex_ai_gateway.tracing._provider", None)

    shutdown_tracing()


async def test_a_semantic_miss_puts_its_nearest_score_on_the_span(
    spans: InMemorySpanExporter,
) -> None:
    """The number ADR-005's threshold is argued from, on the span as a float.

    It arrives at the middleware as the `X-Semantic-Cache-Score` header, which
    is a string; a trace backend cannot sort, filter or histogram a string.
    """
    embedder = ScriptedEmbedder(
        {"user: ping": [1.0, 0.0], "user: pong": [0.0, 1.0]},
    )
    app = create_app(
        settings=Settings(
            _env_file=None,
            tracing_enabled=True,
            cache_enabled=True,
            semantic_cache_enabled=True,
        ),
        provider=MockProvider(),
        redis=aioredis.FakeRedis(),
        embedder=embedder,
    )
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body(messages=[{"role": "user", "content": "ping"}]))
        await client.post(CHAT_URL, json=body(messages=[{"role": "user", "content": "pong"}]))

    scores = [
        dict(s.attributes or {}).get("vortex.semantic_cache.score")
        for s in named(spans, f"POST {CHAT_URL}")
    ]
    # The first request had nothing to compare against; the second was
    # orthogonal to it, which is a miss at a score of zero.
    assert scores == [None, 0.0]


# --- lifespan ---------------------------------------------------------------


async def test_the_app_that_installed_tracing_flushes_it_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flushes: list[int] = []
    monkeypatch.setattr("vortex_ai_gateway.gateway.configure_tracing", lambda settings: True)
    monkeypatch.setattr("vortex_ai_gateway.gateway.shutdown_tracing", lambda: flushes.append(1))
    app = create_app(settings=Settings(_env_file=None), provider=MockProvider())

    async with app.router.lifespan_context(app):
        pass

    assert flushes == [1]


async def test_an_app_that_did_not_install_tracing_leaves_it_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The near miss: a second app in one worker must not stop the first's spans.

    OpenTelemetry's provider is a process global that refuses replacement, so
    the second `create_app` in a worker shares the first's — and flushing it on
    the second app's shutdown would take the first one's tracing down with it.
    """
    flushes: list[int] = []
    monkeypatch.setattr("vortex_ai_gateway.gateway.configure_tracing", lambda settings: False)
    monkeypatch.setattr("vortex_ai_gateway.gateway.shutdown_tracing", lambda: flushes.append(1))
    app = create_app(settings=Settings(_env_file=None), provider=MockProvider())

    async with app.router.lifespan_context(app):
        pass

    assert flushes == []
