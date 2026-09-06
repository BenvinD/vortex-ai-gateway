"""What happens to a request before and after it is served.

Two neighbours, one seam. :mod:`vortex_ai_gateway.ratelimit` owns the buckets
and :mod:`vortex_ai_gateway.spend` owns the ledger; neither knows about the
other, and neither should. This module is the *policy* that uses both, and it
exists so :mod:`vortex_ai_gateway.routes` — which is thin on purpose — has one
object to call twice rather than four collaborators to sequence.

The shape of a metered request:

1. **Admit.** Estimate what it will cost, reserve that against the caller's
   buckets, and either get a decision to attach to the response headers or a
   ``429`` to raise.
2. **Settle.** However it ended, hand back the difference between what was
   reserved and what was actually spent, and write the real numbers to the
   ledger.

Settlement runs on *every* ending, including the failures. A request that never
reached a provider spent nothing, and leaving its reservation held for the rest
of the window would let a broken upstream burn a caller's allowance on calls
that produced nothing.

The one case with no good answer is the request whose usage never arrives — an
abandoned stream, mostly (ADR-019). It cost real money and there is no number
for it. Treating it as free would be a lie the ledger tells forever, so it is
recorded as *unmetered*: counted, published separately, and its reservation kept
rather than refunded, because the estimate is the only evidence there is.
"""

from dataclasses import dataclass
from typing import Final

import structlog

from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest, TokenUsage
from vortex_ai_gateway.error_handling import RateLimitedError
from vortex_ai_gateway.ratelimit import Decision, RateLimiter, estimate_tokens
from vortex_ai_gateway.spend import SpendLedger

logger = structlog.get_logger(__name__)

#: What a throttled caller is told in ``error.code``. Distinct from the
#: ``rate_limit_exceeded`` a provider's own ``429`` arrives with, because one is
#: "you are sending too much" and the other is "our account is", and whoever is
#: paged needs to know which.
GATEWAY_LIMIT_CODE: Final = "gateway_rate_limit_exceeded"


@dataclass(frozen=True, slots=True)
class Reservation:
    """An admitted request, and what it is holding until it settles."""

    principal: Principal
    decision: Decision
    model: str

    @property
    def headers(self) -> dict[str, str]:
        return self.decision.headers()


class Meter:
    """Admission and settlement for one gateway, wired to whatever is configured.

    Both collaborators are optional and independently so: a deployment with no
    Redis has neither, and a deployment that wants a ledger but no limits has
    only the second. ``Meter`` with both set to ``None`` is a working
    pass-through, which is what a local run uses.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        limiter: RateLimiter | None = None,
        ledger: SpendLedger | None = None,
    ) -> None:
        self.settings = settings
        self.limiter = limiter
        self.ledger = ledger

    async def admit(self, principal: Principal, request: ChatCompletionRequest) -> Reservation:
        """Reserve this request's estimated cost, or raise the ``429``.

        The rejection carries both the ``Retry-After`` a client should obey and
        the ``X-RateLimit-*`` headers that say which of the two limits was hit —
        because "slow down" without "on what" leaves a caller guessing between
        halving their concurrency and shortening their prompts.
        """
        estimate = estimate_tokens(request, self.settings)
        if self.limiter is None:
            return Reservation(principal, Decision.unlimited(), request.model)

        decision = await self.limiter.admit(principal, tokens=estimate)
        if not decision.allowed:
            raise RateLimitedError(
                "Rate limit reached for this API key. "
                f"Retry in {decision.retry_after_seconds:.1f}s.",
                code=GATEWAY_LIMIT_CODE,
                headers=decision.headers(),
            )
        return Reservation(principal, decision, request.model)

    async def discard(self, reservation: Reservation) -> None:
        """Release a reservation for a request that produced nothing.

        A provider that refused, timed out, or was refused *by* us generated no
        tokens, so the hold comes back in full and the ledger stays silent — a
        failed request is not usage, and counting it as an unmetered one would
        make every outage look like unexplained spend.
        """
        if self.limiter is not None:
            await self.limiter.reconcile(
                reservation.principal, reserved=reservation.decision.reserved_tokens, actual=0
            )

    async def settle(self, reservation: Reservation, usage: TokenUsage | None) -> None:
        """Release the reservation and record what the request actually cost.

        ``usage`` of ``None`` means nobody knows — see the module docstring. The
        model recorded is the one the *caller* asked for rather than whichever
        upstream served it, because that is the name their own dashboards use
        and the one a fallback hop must not silently change.
        """
        principal = reservation.principal
        if self.limiter is not None and usage is not None:
            await self.limiter.reconcile(
                principal,
                reserved=reservation.decision.reserved_tokens,
                actual=usage.total_tokens,
            )
        if self.ledger is not None:
            await self.ledger.record(
                principal.key_id,
                reservation.model,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                metered=usage is not None,
            )
