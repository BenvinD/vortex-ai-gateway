"""Tool declarations and the caller's control over tool selection."""

from typing import Any, Literal

from pydantic import Field

from vortex_ai_gateway.contracts.base import ContractModel, NonEmptyStr


class FunctionDefinition(ContractModel):
    """A function the model is allowed to call.

    ``parameters`` is a JSON Schema object. It is kept as an opaque mapping on
    purpose: validating the caller's schema against the JSON Schema
    meta-schema belongs to the provider, and rejecting a schema a provider
    would have accepted is worse than passing it through.
    """

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Unique function name; letters, digits, underscores and dashes.",
    )
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: bool | None = Field(
        default=None,
        description="Ask the provider to constrain arguments to 'parameters' exactly.",
    )


class ToolDefinition(ContractModel):
    """A tool offered to the model. Only functions exist today."""

    type: Literal["function"] = "function"
    function: FunctionDefinition


class NamedToolFunction(ContractModel):
    """The name half of a forced tool choice."""

    name: NonEmptyStr


class NamedToolChoice(ContractModel):
    """Force one specific tool for this turn."""

    type: Literal["function"] = "function"
    function: NamedToolFunction


#: ``none`` forbids tools, ``auto`` lets the model decide, ``required`` demands
#: at least one call.
ToolChoiceMode = Literal["none", "auto", "required"]

#: Either a mode or a specific tool.
ToolChoice = ToolChoiceMode | NamedToolChoice
