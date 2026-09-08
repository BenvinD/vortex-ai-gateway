"""Per-key request and token limits, enforced in Redis.

Two limits, one decision. A caller has a requests-per-minute allowance and a
tokens-per-minute allowance, and a request is admitted only if *both* have room.
That "both" is the entire reason this is a Lua script: checking one bucket,
then the other, then debiting them is three round trips with two windows in
which another worker can interleave — and a request rejected by the token
bucket after the request bucket has already been debited has spent an allowance
it never used. Redis runs a script as one command, so the check and the debit
cannot be separated (ADR-021).

**Token bucket, not fixed window.** A per-minute counter that resets on the
minute admits twice the limit across the boundary — the last instant of one minute
and the first of the next. A bucket refills continuously, so the limit means the
same thing wherever the clock happens to be.

**Tokens are reserved, then reconciled.** A request's token cost is knowable
only after the provider answers, so admission debits an *estimate* and
:meth:`RateLimiter.reconcile` settles the difference once the real usage is in.
The estimate is deliberately crude; what matters is that something is held while
the request is in flight, not that the hold is accurate.

**The clock is Python's, and it is the wall clock.** The timestamp goes into the
script as an argument rather than being read with ``TIME`` inside it: a script
that reads the clock itself is non-deterministic, and it cannot be driven by a
test. Wall clock rather than :func:`time.monotonic` — the opposite of
:mod:`vortex_ai_gateway.resilience` — because this state is shared between
processes and machines, which do not share a monotonic origin. Elapsed time is
clamped at zero so an NTP step backwards cannot drain a bucket.

**Redis being down does not stop the gateway.** Every failure here fails *open*
with one warning. A limiter that takes the service down when it cannot enforce a
limit has inverted its own purpose: the limits protect providers from callers,
and a gateway that refuses everything protects nothing.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest

logger = structlog.get_logger(__name__)

#: Emitted once per rejected request, and once per Redis failure. Two events,
#: because "a caller was throttled" is routine and "we cannot throttle anyone"
#: is an incident.
THROTTLED_EVENT: Final = "request throttled"
DEGRADED_EVENT: Final = "rate limiter unavailable; failing open"

#: Both allowances are per *minute*, so a bucket's capacity refills in this many
#: milliseconds. Changing the window means changing what `rpm`/`tpm` mean.
WINDOW_MS: Final = 60_000

#: Rough characters per token. Wrong for every tokenizer and every language,
#: which is fine: it sizes a reservation that is refunded minutes-of-latency
#: later, not a bill. Under-estimating is the safe direction — the reconcile
#: charges the difference.
CHARS_PER_TOKEN: Final = 4

#: How long an untouched bucket survives. One window past full, so a returning
#: caller finds a full bucket rather than a stale one, and an idle key costs
#: nothing.
IDLE_TTL_MS: Final = WINDOW_MS * 2

#: ``{allowed, req_limit, req_remaining, req_reset_ms, tok_limit,
#:   tok_remaining, tok_reset_ms, retry_after_ms}``
#:
#: The loop runs twice, once per bucket, and the two passes matter: nothing is
#: debited until both buckets have been measured. A capacity of zero means the
#: bucket is not configured, and it is skipped entirely rather than treated as a
#: limit of zero — the difference between "unlimited" and "nothing is allowed".
ADMIT_SCRIPT: Final = """
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])

local capacity, level, refill, cost = {}, {}, {}, {}
local allowed = 1
local retry_ms = 0

for i = 1, 2 do
  local base = 2 + (i - 1) * 3
  capacity[i] = tonumber(ARGV[base + 1])
  refill[i] = tonumber(ARGV[base + 2])
  cost[i] = tonumber(ARGV[base + 3])
  if capacity[i] > 0 then
    local stored = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
    local held = tonumber(stored[1])
    local seen = tonumber(stored[2])
    if held == nil or seen == nil then
      held = capacity[i]
      seen = now
    end
    level[i] = math.min(capacity[i], held + math.max(0, now - seen) * refill[i])
    if level[i] < cost[i] then
      allowed = 0
      retry_ms = math.max(retry_ms, (cost[i] - level[i]) / refill[i])
    end
  else
    level[i] = 0
  end
end

local result = {allowed}
for i = 1, 2 do
  if capacity[i] > 0 then
    if allowed == 1 then
      level[i] = level[i] - cost[i]
    end
    redis.call('HSET', KEYS[i], 'tokens', level[i], 'ts', now)
    redis.call('PEXPIRE', KEYS[i], ttl)
    result[#result + 1] = capacity[i]
    result[#result + 1] = math.floor(level[i])
    result[#result + 1] = math.ceil((capacity[i] - level[i]) / refill[i])
  else
    result[#result + 1] = -1
    result[#result + 1] = -1
    result[#result + 1] = 0
  end
end
result[#result + 1] = math.ceil(retry_ms)
return result
"""

#: Give back (or take more of) what a finished request turned out to cost.
#:
#: Clamped at capacity on the way in, because an over-estimate refunded blindly
#: would leave a bucket holding more than it can, and at zero, because an
#: under-estimate must not drive one negative and silently extend the next
#: window's allowance.
SETTLE_SCRIPT: Final = """
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local refill = tonumber(ARGV[4])
local delta = tonumber(ARGV[5])

if capacity <= 0 or delta == 0 then
  return 0
end

local stored = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local held = tonumber(stored[1])
local seen = tonumber(stored[2])
if held == nil or seen == nil then
  held = capacity
  seen = now
end

local level = math.min(capacity, held + math.max(0, now - seen) * refill)
level = math.max(0, math.min(capacity, level + delta))
redis.call('HSET', KEYS[1], 'tokens', level, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl)
return math.floor(level)
"""


def _text_of(content: object) -> str:
    """Whatever text is reachable in a message's content, in any of its shapes.

    Content is a string, or a list of parts of which only some are text. An
    image part contributes tokens too, and a great many of them — but how many
    depends on the vendor and the resolution, so guessing here would be worse
    than the honest under-estimate the reconcile corrects.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        return "".join(getattr(part, "text", "") or "" for part in content)
    return ""


def estimate_tokens(request: ChatCompletionRequest, settings: Settings) -> int:
    """What to reserve for ``request`` before anyone knows what it costs.

    The prompt is in front of us, so it is measured rather than guessed — badly,
    at four characters a token. The completion is not, so it is
    ``max_completion_tokens`` where the caller set one and a configured
    assumption where they did not. ``n`` multiplies the completion half, since
    that is how many of them the provider will generate.
    """
    prompt_chars = sum(len(_text_of(message.content)) for message in request.messages)
    prompt = prompt_chars // CHARS_PER_TOKEN
    completion = request.effective_max_tokens or settings.rate_limit_assumed_completion_tokens
    return max(1, prompt + completion * request.n)


def _format_reset(seconds: float) -> str:
    """A reset delay in the duration format OpenAI's own headers use."""
    if seconds >= 60:
        minutes, remainder = divmod(seconds, 60)
        return f"{int(minutes)}m{int(remainder)}s"
    return f"{seconds:.1f}s"


@dataclass(frozen=True, slots=True)
class BucketState:
    """One allowance, as the caller is told about it.

    A ``limit`` of ``-1`` means this bucket is not configured, and its headers
    are omitted entirely — an unlimited allowance reported as a number would be
    a promise the gateway has not made.
    """

    limit: int
    remaining: int
    reset_seconds: float

    @property
    def configured(self) -> bool:
        return self.limit >= 0


@dataclass(frozen=True, slots=True)
class Decision:
    """The limiter's answer, and everything the caller is told about it."""

    allowed: bool
    requests: BucketState
    tokens: BucketState
    retry_after_seconds: float = 0.0
    reserved_tokens: int = 0

    @classmethod
    def unlimited(cls) -> Decision:
        """The answer when nothing is being enforced — no limits, or no Redis."""
        unset = BucketState(limit=-1, remaining=-1, reset_seconds=0.0)
        return cls(allowed=True, requests=unset, tokens=unset)

    def headers(self) -> dict[str, str]:
        """The ``X-RateLimit-*`` sextet, plus ``Retry-After`` on a rejection.

        Sent on success as well as on rejection, which is the point: a client
        that only learns its remaining allowance by exceeding it can only
        back off *after* being throttled.
        """
        headers: dict[str, str] = {}
        for unit, bucket in (("requests", self.requests), ("tokens", self.tokens)):
            if not bucket.configured:
                continue
            headers[f"x-ratelimit-limit-{unit}"] = str(bucket.limit)
            headers[f"x-ratelimit-remaining-{unit}"] = str(max(0, bucket.remaining))
            headers[f"x-ratelimit-reset-{unit}"] = _format_reset(bucket.reset_seconds)
        if not self.allowed:
            # Ceiling, and never zero: a `Retry-After: 0` invites a client to
            # retry immediately into the same rejection.
            headers["retry-after"] = str(max(1, int(self.retry_after_seconds + 0.999)))
        return headers


class RateLimiter:
    """Per-key request and token buckets, held in Redis.

    One instance per app. The scripts are registered once at construction;
    redis-py sends ``EVALSHA`` and falls back to ``EVAL`` if the server has
    never seen them, so a Redis restart costs one extra round trip rather than
    an error.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = "vortex:rl",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.prefix = prefix
        self._clock = clock
        self._admit = redis.register_script(ADMIT_SCRIPT)
        self._settle = redis.register_script(SETTLE_SCRIPT)

    def _keys(self, key_id: str) -> list[str]:
        return [f"{self.prefix}:{key_id}:requests", f"{self.prefix}:{key_id}:tokens"]

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    async def admit(self, principal: Principal, *, tokens: int) -> Decision:
        """Reserve one request and ``tokens`` tokens, or refuse.

        A principal with no limits never reaches Redis. That is what lets a
        local run — where ``VORTEX_REDIS_URL`` points at a port with nothing
        behind it — work with no Redis at all, rather than working slowly while
        logging a fail-open warning per request.
        """
        if not principal.metered:
            return Decision.unlimited()

        request_keys = self._keys(principal.key_id)
        arguments = [
            self._now_ms(),
            IDLE_TTL_MS,
            principal.rpm,
            principal.rpm / WINDOW_MS,
            1,
            principal.tpm,
            principal.tpm / WINDOW_MS,
            tokens,
        ]
        try:
            raw: list[int] = await self._admit(keys=request_keys, args=arguments)
        except (RedisError, OSError) as exc:
            logger.warning(DEGRADED_EVENT, api_key_id=principal.key_id, error=str(exc))
            return Decision.unlimited()

        decision = Decision(
            allowed=bool(raw[0]),
            requests=BucketState(limit=raw[1], remaining=raw[2], reset_seconds=raw[3] / 1000),
            tokens=BucketState(limit=raw[4], remaining=raw[5], reset_seconds=raw[6] / 1000),
            retry_after_seconds=raw[7] / 1000,
            reserved_tokens=tokens if raw[0] else 0,
        )
        if not decision.allowed:
            logger.warning(
                THROTTLED_EVENT,
                api_key_id=principal.key_id,
                requests_remaining=decision.requests.remaining,
                tokens_remaining=decision.tokens.remaining,
                tokens_requested=tokens,
                retry_after_seconds=round(decision.retry_after_seconds, 3),
            )
        return decision

    async def reconcile(self, principal: Principal, *, reserved: int, actual: int) -> None:
        """Settle the difference between what was held and what was spent.

        Called however the request ended, including when it failed: a request
        that never reached the provider spent no tokens, and holding its
        reservation for the rest of the window would let a broken provider
        exhaust a caller's allowance.
        """
        refund = reserved - actual
        if not principal.metered or principal.tpm <= 0 or refund == 0:
            return
        try:
            await self._settle(
                keys=[self._keys(principal.key_id)[1]],
                args=[
                    self._now_ms(),
                    IDLE_TTL_MS,
                    principal.tpm,
                    principal.tpm / WINDOW_MS,
                    refund,
                ],
            )
        except (RedisError, OSError) as exc:
            logger.warning(DEGRADED_EVENT, api_key_id=principal.key_id, error=str(exc))
