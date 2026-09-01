"""Turning failures into the one error envelope the gateway returns.

Every error path — a malformed body, a missing key, a provider outage — ends
here, so a client only ever has to parse
:class:`~vortex_ai_gateway.contracts.ErrorResponse`. Handlers are registered on
the app in one place rather than scattered across routers.
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from vortex_ai_gateway.contracts import (
    ErrorDetail,
    ErrorType,
    error_response_from_validation_error,
)


class GatewayError(Exception):
    """A failure the gateway itself is reporting, with its HTTP status.

    Raised from dependencies and routes where returning a response is awkward
    (a dependency cannot return one). The handler below renders it; nothing
    else needs to know how the envelope is shaped.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_type: ErrorType,
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = ErrorDetail(message=message, type=error_type, param=param, code=code)


class AuthenticationError(GatewayError):
    """The caller did not present a usable API key."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(
            message,
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_type="authentication_error",
            code=code,
        )


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that render every failure as an envelope."""

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

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        """Render a deliberate failure at the status the raiser chose."""
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if exc.status_code == status.HTTP_401_UNAUTHORIZED
            else None
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail.model_dump(mode="json")},
            headers=headers,
        )
