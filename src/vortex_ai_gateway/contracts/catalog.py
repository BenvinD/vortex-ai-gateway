"""The model-listing contract (``GET /v1/models``).

OpenAI clients call this to discover what they may ask for, so the gateway
answers in the same shape — listing its own routing aliases rather than any one
provider's catalogue.
"""

from typing import Literal

from pydantic import Field

from vortex_ai_gateway.contracts.base import ContractModel, NonEmptyStr, UnixTimestamp


class ModelCard(ContractModel):
    """One model the gateway will accept in ``ChatCompletionRequest.model``."""

    id: NonEmptyStr
    object: Literal["model"] = "model"
    created: UnixTimestamp = 0
    owned_by: NonEmptyStr = Field(description="Provider behind this alias, e.g. 'openai'.")


class ModelList(ContractModel):
    """The envelope OpenAI clients expect around a model listing."""

    object: Literal["list"] = "list"
    data: list[ModelCard] = Field(default_factory=list)
