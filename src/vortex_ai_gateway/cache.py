"""The exact response cache: the same question, answered without asking again.

An entry is keyed on a *canonical* hash of the request rather than on the bytes
the caller sent. The request has already been validated into
:class:`~vortex_ai_gateway.contracts.ChatCompletionRequest` by the time this
module sees it, so hashing the dumped model with its keys sorted and its
defaults filled in makes two spellings of the same question — fields in a
different order, ``temperature`` omitted rather than sent as ``1.0`` — land on
one entry. Hashing the raw body would make a client library's JSON key order
part of the cache key, which is a halved hit rate that nothing reports.

Four fields are dropped before hashing, and only four: ``stream`` and
``stream_options`` describe the framing, ``user`` and ``metadata`` are
caller-defined tags. None of them can change a token the model generates.
Everything else is in the key, ``seed`` and ``n`` included — a caller who
changes a sampling parameter is asking a different question, and this cache
does not have an opinion about how different (ADR-004, and ADR-005 for the one
that will).

**Entries are namespaced by ``key_id``, not shared.** Exact matching means a
caller can only ever receive an answer to a prompt they sent themselves, so
nothing leaks *into* a tenant — but the completion they get back was generated
under another tenant's key and billed to another tenant's account. One
namespace per caller is the default; ``VORTEX_CACHE_SCOPE=global`` is for the
single-tenant deployment where the shared hit rate is the entire point.

**Streaming requests are not cached, in either direction.** They are reported
as ``BYPASS`` rather than ``MISS``, because "we did not look" and "we looked
and it was not there" are different facts and only the second one is a hit rate
worth tuning. See ADR-004 for what replaying a stream would cost.

**Redis being down does not stop the gateway.** Every failure fails open with
one warning and is counted: a lookup that cannot reach Redis is a miss, a store
that cannot reach Redis is a completion nobody kept. The request is already
served either way.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Self

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest, ChatCompletionResponse

logger = structlog.get_logger(__name__)

#: What the caller is told happened. Absent entirely when no cache is
#: configured — a header naming a subsystem that does not exist is noise.
CACHE_HEADER: Final = "x-cache"

#: Set by an operator to get an answer the cache cannot have touched. A
#: dedicated header rather than ``Cache-Control: no-cache``: that one is set by
#: browsers, proxies and reloads for their own reasons, and every one of them
#: would be a bill this gateway did not have to pay.
BYPASS_HEADER: Final = "x-vortex-cache-bypass"

#: Values of :data:`BYPASS_HEADER` that mean "yes". Anything else — including
#: the header sent empty — leaves the cache in play, so a client that always
#: sends the header cannot disable caching by accident.
TRUTHY: Final = frozenset({"1", "true", "yes", "on"})

CacheOutcome = Literal["HIT", "MISS", "BYPASS"]

#: Request fields that cannot change the generated content, and so must not
#: change the cache key.
NON_SEMANTIC_FIELDS: Final = frozenset({"stream", "stream_options", "user", "metadata"})

#: One warning per failed operation, as the limiter does: the request is served
#: regardless, and an operator needs to see that the cache stopped working
#: rather than deduce it from a hit rate that went to zero.
DEGRADED_EVENT: Final = "response cache unavailable; serving uncached"

#: A stored entry that no longer parses is the response contract having moved
#: under it. Discarded rather than repaired — the provider is one call away and
#: the entry expires on its own.
STALE_EVENT: Final = "cached response no longer matches the contract; discarding"


class CacheConfigError(ValueError):
    """A per-route TTL table that cannot be parsed."""


def parse_cache_ttls(spec: str) -> dict[str, int]:
    """Parse ``"/v1/chat/completions=600"`` into a TTL per route path.

    Whitespace around entries is ignored so the value stays readable when a
    deployment manifest wraps it, as ``parse_routes`` does. A TTL of zero is
    meaningful and is *not* the same as an absent entry: it says this route is
    never cached, where an absent one falls back to the global default.
    """
    ttls: dict[str, int] = {}
    for entry in spec.split(","):
        rule = entry.strip()
        if not rule:
            continue
        path, separator, raw_ttl = (part.strip() for part in rule.partition("="))
        if not separator or not path or not raw_ttl:
            raise CacheConfigError(
                f"Cache TTL rule {rule!r} is not 'path=seconds'; "
                "for example '/v1/chat/completions=600'."
            )
        try:
            seconds = int(raw_ttl)
        except ValueError:
            raise CacheConfigError(
                f"Cache TTL rule {rule!r} has a non-integer TTL; seconds, as a whole number."
            ) from None
        if seconds < 0:
            raise CacheConfigError(
                f"Cache TTL rule {rule!r} has a negative TTL; zero means 'never cache this route'."
            )
        ttls[path] = seconds
    return ttls


def canonical_payload(request: ChatCompletionRequest) -> dict[str, Any]:
    """The part of ``request`` that decides what the model would generate.

    Dumped from the validated model rather than read from the request body, so
    defaults are filled in and every value is already in its JSON form.
    """
    payload = request.model_dump(mode="json")
    return {name: value for name, value in payload.items() if name not in NON_SEMANTIC_FIELDS}


def canonical_json(request: ChatCompletionRequest) -> str:
    """``request`` as the one string that stands for every spelling of it.

    ``sort_keys`` is what makes the serialisation stable — a dict's insertion
    order is a property of whoever built it, not of the question being asked —
    and the compact separators keep the digest input free of formatting that
    nobody chose.
    """
    return json.dumps(
        canonical_payload(request),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def request_digest(request: ChatCompletionRequest) -> str:
    """A stable SHA-256 over ``request``, hex-encoded.

    SHA-256 rather than a fast non-cryptographic hash: the digest is the whole
    of the key, so a collision serves one caller's prompt with another's answer,
    and the hashing is nowhere near the cost of the call it is avoiding.
    """
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()


def bypass_requested(headers: Mapping[str, str]) -> bool:
    """Whether this caller asked for the cache to be skipped entirely."""
    return headers.get(BYPASS_HEADER, "").strip().lower() in TRUTHY


@dataclass
class CacheStats:
    """Per-process counters, read by whatever reports on the cache.

    Per *process*, like the circuit breaker's state and for the same reason:
    these say how one worker's cache is behaving, and a fleet-wide number needs
    the counters to live where the entries do. Good enough to answer "is this
    thing working at all", which is the question a new cache actually gets.
    """

    hits: int = 0
    misses: int = 0
    bypasses: int = 0
    stores: int = 0
    errors: int = 0

    @property
    def lookups(self) -> int:
        """Requests the cache was actually asked about, bypasses excluded."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Share of real lookups that were served from the cache."""
        return self.hits / self.lookups if self.lookups else 0.0

    def snapshot(self) -> dict[str, int | float]:
        """The counters as a flat mapping, for a log line or an exporter."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "bypasses": self.bypasses,
            "stores": self.stores,
            "errors": self.errors,
            "hit_rate": round(self.hit_rate, 4),
        }


@dataclass(frozen=True, slots=True)
class Lookup:
    """What the cache found, and what to do with the answer that follows.

    ``key`` and ``ttl`` are carried rather than recomputed so a store never
    re-derives a digest the lookup already has, and so a lookup that decided
    this request is not cacheable makes the store a no-op by construction rather
    than by the caller remembering to check. A key implies a usable TTL: the
    only way to have one is to have passed the check that a zero TTL fails.
    """

    outcome: CacheOutcome | None = None
    response: ChatCompletionResponse | None = None
    key: str | None = None
    ttl: int = 0

    @property
    def headers(self) -> dict[str, str]:
        """The ``X-Cache`` header, or nothing when there is no cache at all."""
        return {CACHE_HEADER: self.outcome} if self.outcome is not None else {}


class ResponseCache:
    """Exact request→response entries in Redis, scoped and TTL'd per route.

    ``redis`` of ``None`` is a working no-op: every lookup reports nothing,
    every store does nothing, and no response carries an ``X-Cache`` header.
    That is what a local run gets, and it is why ``routes.py`` has no branch on
    whether caching is configured.
    """

    def __init__(
        self,
        redis: Redis | None,
        *,
        default_ttl: int = 0,
        ttls: Mapping[str, int] | None = None,
        scope: str = "key",
        prefix: str = "vortex:cache",
    ) -> None:
        self._redis = redis
        self.default_ttl = default_ttl
        self.ttls = dict(ttls or {})
        self.scope = scope
        self.prefix = prefix
        self.stats = CacheStats()

    @classmethod
    def from_settings(cls, settings: Settings, redis: Redis | None) -> Self:
        """The cache a deployment's configuration describes.

        ``redis`` is passed in rather than opened here: the limiter and the
        ledger are on the same connection, and a second pool to the same server
        would double the gateway's connection count to say the same thing.
        """
        return cls(
            redis if settings.cache_enabled else None,
            default_ttl=settings.cache_ttl_seconds,
            ttls=parse_cache_ttls(settings.cache_ttls),
            scope=settings.cache_scope,
        )

    def ttl_for(self, path: str) -> int:
        """How long ``path``'s responses live, zero meaning "do not cache"."""
        return self.ttls.get(path, self.default_ttl)

    def key_for(self, principal: Principal, request: ChatCompletionRequest, path: str) -> str:
        """The Redis key one request occupies, namespaced by caller and route.

        The route is in the key rather than in the digest so two endpoints that
        happen to accept the same body — a completion and a future
        embedding — cannot answer each other.
        """
        namespace = "global" if self.scope == "global" else principal.key_id
        return f"{self.prefix}:{namespace}:{path}:{request_digest(request)}"

    async def lookup(
        self,
        principal: Principal,
        request: ChatCompletionRequest,
        *,
        path: str,
        headers: Mapping[str, str],
    ) -> Lookup:
        """Find a stored answer to ``request``, or say why there is none.

        The three ways this returns without touching Redis are the cache's whole
        policy: a streaming request (ADR-004), a route whose TTL is zero, and a
        caller who asked to be left alone. All three are ``BYPASS``.
        """
        if self._redis is None:
            return Lookup()

        ttl = self.ttl_for(path)
        if request.stream or ttl <= 0 or bypass_requested(headers):
            self.stats.bypasses += 1
            return Lookup(outcome="BYPASS")

        key = self.key_for(principal, request, path)
        try:
            raw = await self._redis.get(key)
        except (RedisError, OSError) as exc:
            self.stats.errors += 1
            logger.warning(DEGRADED_EVENT, operation="lookup", error=str(exc))
            return Lookup(outcome="MISS", key=key, ttl=ttl)

        if raw is None:
            self.stats.misses += 1
            return Lookup(outcome="MISS", key=key, ttl=ttl)

        try:
            response = ChatCompletionResponse.model_validate_json(raw)
        except ValidationError as exc:
            # Validated on the way out rather than trusted, because the entry
            # was written by a build that may no longer be this one.
            self.stats.misses += 1
            logger.warning(STALE_EVENT, error=str(exc))
            return Lookup(outcome="MISS", key=key, ttl=ttl)

        self.stats.hits += 1
        return Lookup(outcome="HIT", response=response, key=key, ttl=ttl)

    async def store(self, lookup: Lookup, response: ChatCompletionResponse) -> None:
        """Keep ``response`` for the request ``lookup`` was made for.

        A no-op unless that lookup produced a key, which is the same thing as
        saying the request was cacheable and the caller did not opt out. The TTL
        comes from the lookup too, so a store cannot disagree with the decision
        that produced it.
        """
        if self._redis is None or lookup.key is None:
            return

        try:
            await self._redis.set(
                lookup.key, response.model_dump_json(exclude_none=True), ex=lookup.ttl
            )
        except (RedisError, OSError) as exc:
            self.stats.errors += 1
            logger.warning(DEGRADED_EVENT, operation="store", error=str(exc))
            return
        self.stats.stores += 1
