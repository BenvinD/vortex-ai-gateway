"""The Prometheus surface: what is counted, and what refuses to be counted.

Three layers again. :class:`LabelBudget` is tested on its own because it is the
only thing here standing between a caller's request body and an unbounded
number of time series. The endpoint tests prove a scrape works and is not
behind the API key. And the recording tests go through the whole app, because
every one of these numbers is produced by a middleware reading a header that
some other subsystem set — asserting on the instrument directly would test the
assertion.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakeredis import aioredis
from fastapi import FastAPI

from tests.test_cache import body, client_for
from tests.test_resilience import ScriptedProvider, resilient
from tests.test_streaming import EndlessProvider, stream_until_disconnect
from tests.upstream import chat_request
from vortex_ai_gateway import metrics
from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    GatewayMetadata,
)
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.metering import Meter
from vortex_ai_gateway.metrics import OVERFLOW, UNKNOWN, LabelBudget
from vortex_ai_gateway.pricing import PriceTable
from vortex_ai_gateway.providers import CannedReply, MockProvider
from vortex_ai_gateway.providers.errors import ProviderBadRequest, ProviderUnavailable
from vortex_ai_gateway.providers.resilience_wrapper import (
    GAVE_UP_BUDGET,
    GAVE_UP_DEADLINE,
    GAVE_UP_EXHAUSTED,
    FallbackProvider,
)
from vortex_ai_gateway.resilience import CircuitBreaker, RetryBudget

CHAT_URL = "/v1/chat/completions"


def build(**overrides: object) -> FastAPI:
    settings = Settings(_env_file=None, **overrides)
    return create_app(settings=settings, provider=MockProvider())


def value(name: str, **labels: str) -> float:
    """One sample from the gateway's registry, as a number rather than ``None``.

    Absent is reported as ``0.0`` on purpose: a Prometheus counter that has
    never been incremented does not exist, and every assertion here wants to
    say "this went up by one" without first caring whether the series was
    already there.
    """
    sample = metrics.REGISTRY.get_sample_value(name, labels)
    return float(sample) if sample is not None else 0.0


# --- the label budget ------------------------------------------------------


def test_a_budget_admits_up_to_its_limit_and_keeps_admitting_those() -> None:
    budget = LabelBudget(2)

    assert budget("gpt-4o") == "gpt-4o"
    assert budget("claude-3") == "claude-3"
    # Full now — but the two it already knows still report as themselves,
    # which is what keeps a dashboard stable once traffic settles.
    assert budget("gpt-4o") == "gpt-4o"
    assert budget("llama3") == OVERFLOW
    assert budget.seen == frozenset({"gpt-4o", "claude-3"})


def test_a_budget_reports_nothing_as_unknown_not_as_overflow() -> None:
    """Two different facts: nobody said, versus too many people said."""
    budget = LabelBudget(1)

    assert budget("") == UNKNOWN
    assert budget(None) == UNKNOWN
    # And an empty value spent none of the budget.
    assert budget("gpt-4o") == "gpt-4o"


def test_a_budget_with_no_room_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="at least one"):
        LabelBudget(0)


def test_a_thousand_distinct_models_allocate_the_budget_and_no_more() -> None:
    """The failure this class exists for, at the scale it would happen at."""
    budget = LabelBudget(50)

    labels = {budget(f"model-{n}") for n in range(1000)}

    assert len(labels) == 51  # the 50 admitted, plus `other`
    assert OVERFLOW in labels


# --- the endpoint ----------------------------------------------------------


async def test_metrics_is_served_without_a_key() -> None:
    """A scraper has no API key, and should not be issued one (ADR-028)."""
    async with client_for(build()) as client:
        response = await client.get("/metrics", headers={})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "vortex_build_info" in response.text


async def test_metrics_can_be_turned_off_entirely() -> None:
    async with client_for(build(metrics_enabled=False)) as client:
        assert (await client.get("/metrics")).status_code == 404


async def test_metrics_can_be_moved_off_the_default_path() -> None:
    app = build(metrics_path="/internal/prom")
    async with client_for(app) as client:
        assert (await client.get("/internal/prom")).status_code == 200
        assert (await client.get("/metrics")).status_code == 404


async def test_build_info_names_the_version_and_environment() -> None:
    async with client_for(build(environment="staging")) as client:
        await client.get("/metrics")

    assert value("vortex_build_info", version="0.1.0", environment="staging") == 1.0


# --- what a request records ------------------------------------------------


async def test_a_served_request_is_counted_by_route_model_and_provider() -> None:
    async with client_for(build()) as client:
        response = await client.post(CHAT_URL, json=body())

    assert response.status_code == 200
    assert (
        value(
            "vortex_requests_total",
            route=CHAT_URL,
            method="POST",
            status="200",
            model="gpt-4o-mini",
            provider="mock",
        )
        == 1.0
    )
    assert (
        value(
            "vortex_request_duration_seconds_count",
            route=CHAT_URL,
            model="gpt-4o-mini",
            provider="mock",
        )
        == 1.0
    )


async def test_the_route_label_is_the_template_not_the_path_that_was_sent() -> None:
    """Otherwise a scanner allocates one time series per URL it guesses at."""
    async with client_for(build()) as client:
        for path in ("/wp-login.php", "/.env", "/admin/config"):
            await client.get(path)

    assert (
        value(
            "vortex_requests_total",
            route="unmatched",
            method="GET",
            status="404",
            model=UNKNOWN,
            provider=UNKNOWN,
        )
        == 3.0
    )


async def test_a_rejected_request_still_names_the_model_that_caused_it() -> None:
    """A 404 labelled `unknown` cannot answer "which model is failing?"."""
    app = create_app(
        settings=Settings(_env_file=None, model_routes="gpt-4o=openai", openai_api_key="k"),
    )
    async with client_for(app) as client:
        response = await client.post(CHAT_URL, json=body(model="not-routed-anywhere"))

    assert response.status_code == 404
    assert (
        value(
            "vortex_requests_total",
            route=CHAT_URL,
            method="POST",
            status="404",
            model="not-routed-anywhere",
            provider="router",
        )
        == 1.0
    )


async def test_the_provider_label_is_corrected_to_whoever_actually_served() -> None:
    """The app mounts a router; the metric should name the adapter (ADR-020)."""

    class Renaming(MockProvider):
        name = "seam"

        async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
            completion = await super().complete(request)
            completion.vortex = GatewayMetadata(provider="anthropic", upstream_model=request.model)
            return completion

    app = create_app(settings=Settings(_env_file=None), provider=Renaming())
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body())

    assert (
        value(
            "vortex_requests_total",
            route=CHAT_URL,
            method="POST",
            status="200",
            model="gpt-4o-mini",
            provider="anthropic",
        )
        == 1.0
    )
    assert (
        value(
            "vortex_requests_total",
            route=CHAT_URL,
            method="POST",
            status="200",
            model="gpt-4o-mini",
            provider="seam",
        )
        == 0.0
    )


async def test_models_past_the_budget_collapse_into_one_series() -> None:
    """The cardinality cap, exercised through the HTTP surface (ADR-027)."""
    async with client_for(build(metrics_label_budget=2)) as client:
        for model in ("first", "second", "third", "fourth"):
            await client.post(CHAT_URL, json=body(model=model))

    counted = {
        model: value(
            "vortex_requests_total",
            route=CHAT_URL,
            method="POST",
            status="200",
            model=model,
            provider="mock",
        )
        for model in ("first", "second", "third", "fourth", OVERFLOW)
    }
    assert counted == {"first": 1.0, "second": 1.0, "third": 0.0, "fourth": 0.0, OVERFLOW: 2.0}


# --- cache outcomes --------------------------------------------------------


async def test_both_cache_tiers_are_counted_from_the_headers_they_reported() -> None:
    """One definition of a hit, shared with the access log (ADR-027)."""
    app = create_app(
        settings=Settings(_env_file=None, cache_enabled=True),
        provider=MockProvider(),
        redis=aioredis.FakeRedis(),
    )
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body())  # MISS, then stored
        await client.post(CHAT_URL, json=body())  # HIT

    assert value("vortex_cache_lookups_total", tier="exact", outcome="MISS") == 1.0
    assert value("vortex_cache_lookups_total", tier="exact", outcome="HIT") == 1.0
    # No semantic tier is configured, so it contributes nothing at all — not a
    # run of zeros that would drag somebody else's hit rate down.
    assert value("vortex_cache_lookups_total", tier="semantic", outcome="MISS") == 0.0


async def test_a_gateway_with_no_cache_counts_no_lookups() -> None:
    async with client_for(build()) as client:
        await client.post(CHAT_URL, json=body())

    for outcome in ("HIT", "MISS", "BYPASS"):
        assert value("vortex_cache_lookups_total", tier="exact", outcome=outcome) == 0.0


# --- streams ---------------------------------------------------------------


async def test_ttft_is_recorded_for_a_stream_and_not_for_a_buffered_reply() -> None:
    """TTFT is only meaningful where there is a *first* token to be early.

    A buffered response's first byte is its last byte, so timing it would
    publish the full duration under a name that means something else — and on
    the same graph as the streams, where it is the slowest thing on it.
    """
    async with client_for(build()) as client:
        await client.post(CHAT_URL, json=body())
        async with client.stream("POST", CHAT_URL, json=body(stream=True)) as response:
            async for _ in response.aiter_bytes():
                pass

    labels = {"model": "gpt-4o-mini", "provider": "mock"}
    assert value("vortex_stream_ttft_seconds_count", **labels) == 1.0
    assert value("vortex_request_duration_seconds_count", route=CHAT_URL, **labels) == 2.0


@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_a_stream_is_counted_by_how_it_ended(outcome: str) -> None:
    replies: tuple[Any, ...] = (
        (CannedReply("hello there"),)
        if outcome == "completed"
        else (ProviderUnavailable("down", provider="mock"),)
    )
    app = create_app(settings=Settings(_env_file=None), provider=MockProvider(replies=replies))
    async with client_for(app) as client:
        async with client.stream("POST", CHAT_URL, json=body(stream=True)) as response:
            async for _ in response.aiter_bytes():
                pass

    assert (
        value("vortex_streams_total", provider="mock", model="gpt-4o-mini", outcome=outcome) == 1.0
    )


async def test_an_abandoned_stream_is_counted_as_abandoned() -> None:
    """The third ending, and the only one with a cost and no delivery (ADR-019).

    Parametrising the two endings above and leaving this one out is exactly the
    gap `tests/CLAUDE.md` records: an abandonment runs its bookkeeping inside a
    scope that is already cancelled.
    """
    app = create_app(settings=Settings(_env_file=None), provider=EndlessProvider())

    await stream_until_disconnect(app, json.dumps(body(stream=True)).encode(), after=3)

    assert (
        value("vortex_streams_total", provider="endless", model="gpt-4o-mini", outcome="abandoned")
        == 1.0
    )


# --- resilience ------------------------------------------------------------


async def test_every_attempt_is_counted_so_amplification_is_visible(
    sleeps: list[float],
) -> None:
    """The number Day 5 wanted: upstream calls per request served (ADR-001)."""
    inner = ScriptedProvider(
        ProviderUnavailable("down", provider="primary"),
        ProviderUnavailable("down", provider="primary"),
        None,
    )

    await resilient(inner).complete(chat_request())

    assert value("vortex_provider_attempts_total", provider="primary", outcome="error") == 2.0
    assert value("vortex_provider_attempts_total", provider="primary", outcome="ok") == 1.0
    assert (
        value("vortex_provider_retries_total", provider="primary", error="ProviderUnavailable")
        == 2.0
    )


async def test_a_non_retryable_failure_is_one_attempt_and_no_retries() -> None:
    """A 400 cost a call and must not read as a retry storm."""
    inner = ScriptedProvider(ProviderBadRequest("unknown model", provider="primary"))

    with pytest.raises(ProviderBadRequest):
        await resilient(inner).complete(chat_request())

    assert value("vortex_provider_attempts_total", provider="primary", outcome="error") == 1.0
    assert (
        value("vortex_provider_retries_total", provider="primary", error="ProviderBadRequest")
        == 0.0
    )


@pytest.mark.parametrize(
    ("reason", "overrides"),
    [
        (GAVE_UP_EXHAUSTED, {}),
        (GAVE_UP_BUDGET, {"budget": RetryBudget(capacity=0, refill_per_second=0.0)}),
        (GAVE_UP_DEADLINE, {"deadline_seconds": 0.0}),
    ],
    ids=["attempts", "budget", "deadline"],
)
async def test_the_three_ways_to_give_up_stay_distinguishable(
    sleeps: list[float], reason: str, overrides: dict[str, object]
) -> None:
    """Each means something different is wrong; one counter would hide which."""
    inner = ScriptedProvider(ProviderUnavailable("down", provider="primary"))

    with pytest.raises(ProviderUnavailable):
        await resilient(inner, **overrides).complete(chat_request())

    assert value("vortex_provider_give_ups_total", provider="primary", reason=reason) == 1.0


async def test_a_fallback_hop_is_counted_from_and_to() -> None:
    down = ScriptedProvider(ProviderUnavailable("down", provider="primary"), name="primary")
    healthy = ScriptedProvider(name="secondary")

    await FallbackProvider([down, healthy]).complete(chat_request())

    assert value("vortex_fallbacks_total", from_provider="primary", to_provider="secondary") == 1.0


async def test_the_breaker_publishes_a_gauge_and_a_running_count() -> None:
    """The gauge says what is happening now; the counter survives recovery."""
    breaker = CircuitBreaker(name="primary", failure_threshold=1, open_seconds=30.0)

    await breaker.record_failure()

    assert value("vortex_breaker_state", provider="primary") == 2.0  # open
    assert value("vortex_breaker_transitions_total", provider="primary", state="open") == 1.0


# --- tokens and money ------------------------------------------------------


async def test_a_settled_request_counts_its_tokens_and_what_they_cost() -> None:
    """Both, from one settlement, on a gateway with no Redis at all."""
    app = create_app(
        settings=Settings(_env_file=None),
        provider=MockProvider(replies=(CannedReply("one two three"),)),
    )
    async with client_for(app) as client:
        response = await client.post(CHAT_URL, json=body(model="gpt-4o-mini"))

    usage = response.json()["usage"]
    assert (
        value("vortex_tokens_total", model="gpt-4o-mini", kind="prompt") == usage["prompt_tokens"]
    )
    assert (
        value("vortex_tokens_total", model="gpt-4o-mini", kind="completion")
        == usage["completion_tokens"]
    )
    assert value("vortex_cost_usd_total", model="gpt-4o-mini") > 0.0


async def test_an_unpriced_model_reports_tokens_and_no_dollars() -> None:
    """ADR-022's rule as a metric: a gap in the table is not a price of zero.

    A cost graph reading zero for a model that is in production is exactly how
    a new model gets rolled out and billed to nobody, so the series is left
    absent — which a dashboard draws as a hole and an operator can see.
    """
    app = create_app(
        settings=Settings(_env_file=None),
        provider=MockProvider(replies=(CannedReply("hi"),)),
    )
    async with client_for(app) as client:
        await client.post(CHAT_URL, json=body(model="model-nobody-priced"))

    assert value("vortex_tokens_total", model="model-nobody-priced", kind="prompt") > 0.0
    assert (
        metrics.REGISTRY.get_sample_value("vortex_cost_usd_total", {"model": "model-nobody-priced"})
        is None
    )


async def test_an_unmetered_settlement_adds_no_tokens_at_all() -> None:
    """A stream nobody has a count for has no honest number to add.

    The estimate is *not* used: it would land on the same graph as the
    measurements with nothing to tell them apart, which is the confusion the
    ledger publishes unmetered requests separately to avoid (ADR-019, ADR-022).
    """
    meter = Meter(
        Settings(_env_file=None), prices=PriceTable.from_settings(Settings(_env_file=None))
    )
    reservation = await meter.admit(Principal(key_id="k"), chat_request())

    await meter.settle(reservation, None)

    assert value("vortex_tokens_total", model=chat_request().model, kind="prompt") == 0.0


# --- the dashboard -----------------------------------------------------------

#: The committed Grafana dashboard, found from this file rather than from the
#: installed package: it is repository configuration, not something the wheel
#: ships.
DASHBOARD = Path(__file__).resolve().parents[1] / "deploy/grafana/dashboards/vortex-gateway.json"

#: Suffixes ``prometheus_client`` derives from a histogram, which a PromQL
#: query names directly and no instrument declares.
DERIVED = ("_bucket", "_count", "_sum")


def declared_metric_names() -> set[str]:
    """Every name a PromQL query could legitimately use.

    Read out of the rendered exposition rather than off the instrument objects,
    because the rendered name is the one a query has to match: a counter
    declared ``vortex_requests_total`` is held internally as ``vortex_requests``
    and published with the suffix back on.
    """
    body, _ = metrics.render()
    names: set[str] = set()
    for line in body.decode().splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split()
            names.add(name)
            if kind == "histogram":
                names |= {f"{name}{suffix}" for suffix in DERIVED}
    return names


def test_every_metric_the_dashboard_queries_actually_exists() -> None:
    """The one thing about the stack that is checkable without running it.

    `compose.yaml` and the Grafana provisioning cannot be exercised in CI, so
    the dashboard's most likely failure — a panel querying a metric that was
    renamed, or never existed — would otherwise be found by a person opening
    Grafana and reading "No data" on one panel out of eighteen.
    """
    dashboard = json.loads(DASHBOARD.read_text())
    queried = {
        name
        for panel in dashboard["panels"]
        for target in panel["targets"]
        for name in re.findall(r"\bvortex_[a-z_]+\b", target["expr"])
    }

    assert queried, "the dashboard asks for nothing at all"
    assert queried <= declared_metric_names()


def test_the_dashboard_points_at_the_provisioned_datasource() -> None:
    """The datasource is referenced by a UID that `deploy/` has to pin.

    Letting Grafana generate one would make every panel in the committed file
    point at a datasource that does not exist yet.
    """
    dashboard = json.loads(DASHBOARD.read_text())
    provisioned = yaml.safe_load(
        (DASHBOARD.parents[1] / "provisioning/datasources/prometheus.yml").read_text()
    )
    uids = {
        target["datasource"]["uid"] for panel in dashboard["panels"] for target in panel["targets"]
    }

    assert uids == {source["uid"] for source in provisioned["datasources"]}
