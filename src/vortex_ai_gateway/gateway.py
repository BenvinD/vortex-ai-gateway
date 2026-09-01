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

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from vortex_ai_gateway.config import Settings, get_settings
from vortex_ai_gateway.contracts import error_response_from_validation_error
from vortex_ai_gateway.logging_config import configure_logging
from vortex_ai_gateway.middleware import RequestIDMiddleware

#: A readiness check raises to signal "not ready"; returning means "ok".
ReadinessCheck = Callable[[], Awaitable[None]]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    ``settings`` defaults to the process-wide config read from the environment;
    tests pass an explicit :class:`~vortex_ai_gateway.config.Settings` to avoid
    depending on the ambient environment or a local ``.env``.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Vortex AI Gateway",
        version="0.1.0",
        description="Multi-provider LLM gateway with routing and guardrails",
    )
    app.state.settings = settings
    app.add_middleware(RequestIDMiddleware)

    @app.exception_handler(RequestValidationError)
    async def handle_invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Report a malformed request in the OpenAI error envelope.

        FastAPI's default is a ``422`` carrying pydantic's raw error list; an
        OpenAI client understands neither. Translating to ``400`` plus
        ``{"error": {...}}`` means an existing client surfaces the real reason
        instead of a generic transport failure.
        """
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_response_from_validation_error(exc.errors()).model_dump(mode="json"),
        )

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
