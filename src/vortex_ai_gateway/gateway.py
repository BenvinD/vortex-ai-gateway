"""Core gateway implementation.

Health endpoints follow the Kubernetes probe split:

* ``/healthz`` is a *liveness* probe. It answers "is this process alive?"
  and deliberately checks nothing external. A failure tells the orchestrator
  to **restart the container**.
* ``/readyz`` is a *readiness* probe. It answers "can this instance serve
  useful traffic right now?" by running the dependency checks a real request
  needs (Redis, upstream providers, warm caches). A failure returns ``503`` so
  the load balancer **stops routing to this instance** without it being killed.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Response, status
from redis.asyncio import Redis

from vortex_ai_gateway.config import Settings, get_settings
from vortex_ai_gateway.error_handling import install_error_handlers
from vortex_ai_gateway.keys import KeyStore
from vortex_ai_gateway.logging_config import configure_logging
from vortex_ai_gateway.metering import Meter
from vortex_ai_gateway.middleware import RequestIDMiddleware
from vortex_ai_gateway.pricing import PriceTable
from vortex_ai_gateway.providers import ChatProvider, MockProvider
from vortex_ai_gateway.ratelimit import RateLimiter
from vortex_ai_gateway.routes import create_chat_router
from vortex_ai_gateway.routing import build_router
from vortex_ai_gateway.spend import SpendLedger

#: A readiness check raises to signal "not ready"; returning means "ok".
ReadinessCheck = Callable[[], Awaitable[None]]


def create_app(
    settings: Settings | None = None,
    provider: ChatProvider | None = None,
    redis: Redis | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    ``settings`` defaults to the process-wide config read from the environment;
    tests pass an explicit :class:`~vortex_ai_gateway.config.Settings` to avoid
    depending on the ambient environment or a local ``.env``.

    ``provider`` serves ``/v1/chat/completions``. Left out, it is built from
    the routing table in ``settings`` (ADR-016); with no table configured it
    falls back to :class:`~vortex_ai_gateway.providers.mock.MockProvider` so
    the endpoint is exercisable end to end with no key and no bill — a
    substitution loud enough to be caught in the logs, since a deployment
    answering from canned replies is the worst failure this service could have.

    ``redis`` backs the rate limiter and the usage ledger. Left out, it is
    opened from ``settings.redis_url`` when ``metering_enabled`` is set and
    otherwise not opened at all — the gateway runs with no Redis, no limits and
    no ledger, which is what a laptop wants (ADR-021).

    Anything the factory built is closed on shutdown; anything passed in belongs
    to the caller and is left alone.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    owns_redis = redis is None and settings.metering_enabled
    if owns_redis:
        redis = Redis.from_url(settings.redis_url)

    owns_provider = provider is None
    if provider is None:
        provider = build_router(settings)
    if provider is None:
        provider = MockProvider()
        structlog.get_logger(__name__).warning(
            "no provider configured; serving canned replies",
            provider=provider.name,
            environment=settings.environment,
        )

    served_by = provider
    connection = redis

    # Both halves of metering hang off one Redis connection, and both are absent
    # together: there is no deployment that wants limits but no ledger, and
    # keeping them on one switch means one thing to reason about when Redis is
    # sick. Deliberately *not* registered as a readiness check — the limiter
    # fails open, so draining a working instance because Redis is down would
    # turn a degradation into an outage.
    meter = Meter(
        settings,
        limiter=RateLimiter(connection) if connection is not None else None,
        ledger=(
            SpendLedger(
                connection,
                PriceTable.from_settings(settings),
                retention_days=settings.usage_retention_days,
            )
            if connection is not None
            else None
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Hold the app open, then release the connection pools we opened."""
        yield
        closer = getattr(served_by, "aclose", None) if owns_provider else None
        if closer is not None:
            await closer()
        if owns_redis and connection is not None:
            await connection.aclose()

    app = FastAPI(
        title="Vortex AI Gateway",
        version="0.1.0",
        description="Multi-provider LLM gateway with routing and guardrails",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.provider = provider
    # Opened here rather than per request: the constructor creates the file and
    # the schema, and doing that on the hot path would race the CLI. `None`
    # means the deployment has no key database, which `auth` reads as "fall
    # back to the environment allow-list" (ADR-003).
    app.state.key_store = KeyStore(settings.key_db_path) if settings.key_db_path else None
    app.state.meter = meter
    app.state.redis = connection

    app.add_middleware(RequestIDMiddleware)
    install_error_handlers(app)
    app.include_router(create_chat_router(provider, meter))

    # Dependency probes register here as subsystems come online. Redis is
    # pointedly not one: a subsystem belongs here only if the gateway cannot
    # serve useful traffic without it, and the limiter and ledger both degrade
    # rather than fail. Redis-backed *load balancing* would be a different
    # answer. Exposed on ``app.state`` so tests and later wiring can add to it
    # without reaching into the closure.
    readiness_checks: dict[str, ReadinessCheck] = {}
    app.state.readiness_checks = readiness_checks

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe: the process is up and the event loop is turning.

        Never touches Redis, providers, or config. If a dependency outage
        could make this return non-200, the orchestrator would restart a
        perfectly healthy process and make the outage worse.
        """
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        """Readiness probe: every dependency this instance needs is usable.

        Runs each registered check; on any failure it returns ``503`` with a
        per-check breakdown so the load balancer drains this instance until
        the dependency recovers.
        """
        results: dict[str, str] = {}
        for name, check in readiness_checks.items():
            try:
                await check()
            except Exception as exc:
                results[name] = f"error: {exc}"
            else:
                results[name] = "ok"

        if any(result != "ok" for result in results.values()):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not ready"} | results
        return {"status": "ready"} | results

    return app


app = create_app()
