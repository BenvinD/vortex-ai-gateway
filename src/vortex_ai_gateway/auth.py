"""Client authentication at the edge.

A stub, deliberately: it establishes *where* auth happens and what a rejection
looks like, without pretending to be key management. Real keys will be hashed,
stored, scoped and rotated (ADR-003); none of that changes the shape of this
seam or the 401 a client sees.
"""

from fastapi import Request

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.error_handling import AuthenticationError

BEARER_PREFIX = "Bearer "


def _extract_bearer_token(header: str | None) -> str:
    """Pull the token out of an ``Authorization`` header, or reject it.

    The two failures are kept distinct — no header at all versus a header the
    gateway cannot read — because they are different bugs at the caller: a
    missing config value versus a malformed one.
    """
    if header is None or not header.strip():
        raise AuthenticationError(
            "No API key provided. Send it as 'Authorization: Bearer <key>'.",
            code="missing_api_key",
        )
    if not header.startswith(BEARER_PREFIX):
        raise AuthenticationError(
            "Malformed Authorization header. Expected 'Bearer <key>'.",
            code="invalid_authorization_header",
        )
    token = header.removeprefix(BEARER_PREFIX).strip()
    if not token:
        raise AuthenticationError(
            "No API key provided. Send it as 'Authorization: Bearer <key>'.",
            code="missing_api_key",
        )
    return token


async def require_api_key(request: Request) -> str:
    """Authenticate the caller, returning the key it presented.

    With no keys configured the gateway is in development mode and accepts any
    well-formed key — but still rejects a request that carries none, so the
    401 path is exercised by default rather than discovered in production.
    """
    token = _extract_bearer_token(request.headers.get("authorization"))

    settings: Settings = request.app.state.settings
    allowed = settings.allowed_api_keys
    if allowed and token not in allowed:
        raise AuthenticationError(
            "Incorrect API key provided.",
            code="invalid_api_key",
        )

    request.state.api_key = token
    return token
