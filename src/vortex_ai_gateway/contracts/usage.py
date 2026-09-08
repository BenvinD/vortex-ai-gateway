"""Token accounting and log-probability payloads returned with a completion."""

from pydantic import Field

from vortex_ai_gateway.contracts.base import ContractModel


class PromptTokensDetails(ContractModel):
    """Breakdown of the prompt half of a completion's token bill."""

    cached_tokens: int = Field(default=0, ge=0)
    audio_tokens: int = Field(default=0, ge=0)


class CompletionTokensDetails(ContractModel):
    """Breakdown of the generated half, including tokens never shown."""

    reasoning_tokens: int = Field(default=0, ge=0)
    audio_tokens: int = Field(default=0, ge=0)
    accepted_prediction_tokens: int = Field(default=0, ge=0)
    rejected_prediction_tokens: int = Field(default=0, ge=0)


class TokenUsage(ContractModel):
    """What the request cost, in the unified vocabulary.

    Providers report usage under different names and with different
    granularity; adapters normalise into this shape, filling the detail models
    only where the upstream actually reported them. ``total_tokens`` is carried
    as reported rather than recomputed, so a provider disagreeing with itself
    stays visible instead of being papered over.
    """

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    prompt_tokens_details: PromptTokensDetails | None = None
    completion_tokens_details: CompletionTokensDetails | None = None


class TopLogprob(ContractModel):
    """One alternative token the model considered at a position."""

    token: str
    logprob: float
    bytes: list[int] | None = None


class TokenLogprob(TopLogprob):
    """A chosen token plus the alternatives that lost to it."""

    top_logprobs: list[TopLogprob] = Field(default_factory=list)


class ChoiceLogprobs(ContractModel):
    """Per-choice log probabilities, present only when ``logprobs`` was set."""

    content: list[TokenLogprob] | None = None
    refusal: list[TokenLogprob] | None = None
