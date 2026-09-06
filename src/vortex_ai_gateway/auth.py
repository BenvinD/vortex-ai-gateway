"""Client authentication at the edge, and the identity it produces.

Authentication answers one question — *who is calling?* — and everything
downstream of it is built on the answer. A rate limit needs something to count
against, a bill needs something to attach to, and neither may be the secret the
caller sent. So this module does not return a token; it returns a
:class:`Principal`, whose ``key_id`` is public, stable, and safe to put in a
Redis key or a log line.

There are three sources of keys, checked in this order, and only one is ever
active:

1. **A key store** (``VORTEX_KEY_DB_PATH``): hashed, revocable, named, with
   per-key limits. See :mod:`vortex_ai_gateway.keys` and ADR-003.
2. **The environment allow-list** (``VORTEX_API_KEYS``): plaintext, no limits,
   no names. Fine for one key and a local run.
3. **Development mode** (neither set): any well-formed key is accepted, but a
   request carrying *no* key is still rejected — so the 401 path is exercised
   by default rather than discovered in production.

They are not merged. Two allow-lists means revoking a key from one and still
being let in by the other.
"""

from dataclasses import dataclass

import structlog
from fastapi import Request

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.error_handling import AuthenticationError
from vortex_ai_gateway.keys import ApiKeyRecord, KeyStore, fingerprint, parse_key_id

logger = structlog.get_logger(__name__)

BEARER_PREFIX = "Bearer "

#: Logged whenever a well-formed key is turned away, so a caller reporting "it
#: stopped working" can be answered from the gateway's own records.
REJECTED_EVENT = "api key rejected"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, as everything downstream sees it.

    ``key_id`` is the public half of a stored key, or a truncated digest of the
    presented token when there is no store behind it. Either way it identifies
    the caller across restarts without being usable as a credential — which
    matters, because it is about to become part of a Redis key and a log field.

    ``rpm``/``tpm`` of zero mean *unlimited*, and that is load-bearing rather
    than lazy: it is what lets a gateway with no rate limits configured never
    open a Redis connection at all.
    """

    key_id: str
    name: str = ""
    rpm: int = 0
    tpm: int = 0

    @property
    def metered(self) -> bool:
        """Whether any limit applies, and so whether the limiter runs."""
        return self.rpm > 0 or self.tpm > 0


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


def _reject(token: str) -> AuthenticationError:
    """The one rejection every failed key gets, plus a log line naming which.

    The caller is told the same thing whether the key is unknown, revoked, or
    simply wrong, because telling them apart is telling an attacker which key
    IDs exist. The gateway's own logs get the public ID, which is the half an
    operator needs and the half that is not a secret.
    """
    logger.warning(REJECTED_EVENT, api_key_id=parse_key_id(token) or fingerprint(token))
    return AuthenticationError("Incorrect API key provided.", code="invalid_api_key")


def _from_record(record: ApiKeyRecord, settings: Settings) -> Principal:
    """A stored key's identity, with the deployment's defaults filled in.

    A per-key limit wins; zero on the record means "whatever the deployment
    says", which may itself be unlimited. The ladder has one rung more than it
    looks like it needs so that raising a fleet-wide default does not require
    rewriting every key that never wanted a custom limit.
    """
    return Principal(
        key_id=record.key_id,
        name=record.name,
        rpm=record.rpm or settings.rate_limit_default_rpm,
        tpm=record.tpm or settings.rate_limit_default_tpm,
    )


def _from_token(token: str, settings: Settings) -> Principal:
    """The identity of a key with no record behind it.

    Environment-list and development-mode keys are still metered and still
    billed; they just have nothing to hang a *per-key* limit on, so they take
    the deployment defaults and a digest for a name.
    """
    return Principal(
        key_id=fingerprint(token),
        rpm=settings.rate_limit_default_rpm,
        tpm=settings.rate_limit_default_tpm,
    )


async def require_api_key(request: Request) -> Principal:
    """Authenticate the caller and return who they are.

    Also left on ``request.state``: ``principal`` for the limiter and the
    ledger, and ``api_key`` for anything that still wants the raw token. The
    token stops travelling any further than this — nothing downstream needs it,
    and every extra place it is carried is another place it can be logged.
    """
    token = _extract_bearer_token(request.headers.get("authorization"))
    settings: Settings = request.app.state.settings
    store: KeyStore | None = getattr(request.app.state, "key_store", None)

    if store is not None:
        record = await store.authenticate(token)
        if record is None:
            raise _reject(token)
        principal = _from_record(record, settings)
    else:
        allowed = settings.allowed_api_keys
        if allowed and token not in allowed:
            raise _reject(token)
        principal = _from_token(token, settings)

    request.state.api_key = token
    request.state.principal = principal
    structlog.contextvars.bind_contextvars(api_key_id=principal.key_id)
    return principal
