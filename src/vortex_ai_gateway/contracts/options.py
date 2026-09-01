"""Per-request output options: response format and streaming behaviour."""

from typing import Annotated, Any, Literal

from pydantic import Field

from vortex_ai_gateway.contracts.base import ContractModel, NonEmptyStr


class TextResponseFormat(ContractModel):
    """Free-form text. The default when nothing is requested."""

    type: Literal["text"] = "text"


class JsonObjectResponseFormat(ContractModel):
    """Any syntactically valid JSON object, with no schema attached."""

    type: Literal["json_object"] = "json_object"


class JsonSchemaDefinition(ContractModel):
    """The schema half of a ``json_schema`` response format."""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = None
    # `schema` shadows a deprecated BaseModel method, so the field is named
    # `json_schema` and aliased onto the wire key. Dump with `by_alias=True`.
    json_schema: dict[str, Any] = Field(alias="schema")
    strict: bool | None = None


class JsonSchemaResponseFormat(ContractModel):
    """JSON constrained to a caller-supplied schema."""

    type: Literal["json_schema"] = "json_schema"
    json_schema: JsonSchemaDefinition


#: Discriminated on ``type`` so an unknown format names itself in the error.
ResponseFormat = Annotated[
    TextResponseFormat | JsonObjectResponseFormat | JsonSchemaResponseFormat,
    Field(discriminator="type"),
]


class StreamOptions(ContractModel):
    """Extras that only apply while streaming."""

    include_usage: bool = Field(
        default=False,
        description="Emit a final chunk carrying token usage after the last delta.",
    )


class PredictedOutput(ContractModel):
    """Content the caller expects the model to reproduce verbatim.

    Providers that support it use this to skip re-generating unchanged text —
    the common case being an edit to a large file.
    """

    type: Literal["content"] = "content"
    content: NonEmptyStr | Annotated[list[NonEmptyStr], Field(min_length=1)]
