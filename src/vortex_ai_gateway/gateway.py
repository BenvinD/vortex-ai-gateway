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

from vortex_ai_gateway.config import Settings, get_settings
from vortex_ai_gateway.error_handling import install_error_handlers
from vortex_ai_gateway.logging_config import configure_logging
from vortex_ai_gateway.middleware import RequestIDMiddleware
from vortex_ai_gateway.providers import ChatProvider, MockProvider
from vortex_ai_gateway.routes import create_chat_router
from vortex_ai_gateway.routing import build_router

#: A readiness check raises to signal "not ready"; returning means "ok".
ReadinessCheck = Callable[[], Awaitable[None]]


def create_app(
    settings: Settings | None = None,
    provider: ChatProvider | None = None,
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

    A provider the factory built is closed on shutdown; one passed in belongs
    to the caller and is left alone.
    """
    settings = settings or get_settings()
    configure_logging(settings)

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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Hold the app open, then release the upstream connection pools."""
        yield
        closer = getattr(served_by, "aclose", None) if owns_provider else None
        if closer is not None:
            await closer()

    app = FastAPI(
        title="Vortex AI Gateway",
        version="0.1.0",
        description="Multi-provider LLM gateway with routing and guardrails",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.provider = provider

    app.add_middleware(RequestIDMiddleware)
    install_error_handlers(app)
    app.include_router(create_chat_router(provider))

    # Dependency probes register here as subsystems come online, e.g. a Redis
    # PING once the load-balancer backend exists. Exposed on ``app.state`` so
    # tests (and later, wiring code) can add to it without reaching into the
    # closure.
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
