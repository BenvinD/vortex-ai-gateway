"""The usage-report contract (``GET /v1/usage``).

Gateway-native rather than OpenAI-shaped, and deliberately so: OpenAI's own
usage API answers about an *organisation's* account, and this answers about the
key that asked. Anything a caller can read here is something they generated.

Costs are :class:`~decimal.Decimal` and therefore serialise as JSON *strings*.
``"0.0043750"`` survives a round trip; ``0.0043750`` becomes a binary float the
moment any JSON parser touches it, and a caller reconciling against an invoice
would find their sum disagreeing with ours in the last places. A client that
wants a number can make one; a client that wants the exact figure cannot get it
back once we have thrown it away.
"""

from decimal import Decimal
from typing import Literal

from pydantic import Field

from vortex_ai_gateway.contracts.base import ContractModel, NonEmptyStr


class ModelUsage(ContractModel):
    """What one model cost one key on one day.

    ``cost_usd`` is ``None`` when the price table has no rule for the model.
    That is not the same as zero, and reporting it as zero is how an unpriced
    model gets rolled out and billed to nobody.

    ``unmetered_requests`` counts the requests inside ``requests`` whose token
    usage never arrived — a stream the client abandoned before the final chunk,
    or a provider that answered without reporting any. Those cost real money and
    contribute nothing to the totals above, so the count is published rather
    than hidden: a bill that disagrees with this report should be explicable
    from this field.
    """

    model: NonEmptyStr
    requests: int = Field(ge=0)
    unmetered_requests: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: Decimal | None = None


class DailyUsage(ContractModel):
    """One day's totals, broken down by model."""

    date: NonEmptyStr = Field(description="UTC calendar day, as YYYY-MM-DD.")
    requests: int = Field(ge=0)
    unmetered_requests: int = Field(default=0, ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: Decimal | None = None
    models: list[ModelUsage] = Field(default_factory=list)


class UsageReport(ContractModel):
    """Everything ``GET /v1/usage`` says about the calling key.

    ``unpriced_models`` is what keeps ``total_cost_usd`` honest: the total is
    the sum of what *could* be priced, and this names what was left out of it,
    so a figure that looks too low explains itself.
    """

    object: Literal["usage.report"] = "usage.report"
    key_id: NonEmptyStr
    start_date: NonEmptyStr
    end_date: NonEmptyStr
    total_requests: int = Field(default=0, ge=0)
    total_unmetered_requests: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    total_cost_usd: Decimal | None = None
    unpriced_models: list[str] = Field(default_factory=list)
    daily: list[DailyUsage] = Field(default_factory=list)
