"""Conversation messages: roles, multimodal content parts, and tool calls.

The message list is the part of the contract with the most structure, so it
gets its own module. Messages form a discriminated union on ``role``: pydantic
picks the right member from that one key, which turns "wrong shape" into an
error naming the role rather than a wall of five failed alternatives.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from vortex_ai_gateway.contracts.base import ContractModel, NonEmptyStr

#: Every role the contract accepts. ``developer`` is OpenAI's replacement for
#: ``system`` on newer models; both are accepted and carried through.
Role = Literal["system", "developer", "user", "assistant", "tool"]


class TextContentPart(ContractModel):
    """A run of plain text inside a multimodal message."""

    type: Literal["text"] = "text"
    text: str


class ImageUrl(ContractModel):
    """An image reference: either an ``http(s)`` URL or a ``data:`` URI."""

    url: NonEmptyStr
    detail: Literal["auto", "low", "high"] = "auto"


class ImageContentPart(ContractModel):
    """An image attached to a user message."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageUrl


class InputAudio(ContractModel):
    """Base64-encoded audio supplied inline by the caller."""

    data: NonEmptyStr
    format: Literal["wav", "mp3"]


class AudioContentPart(ContractModel):
    """An audio clip attached to a user message."""

    type: Literal["input_audio"] = "input_audio"
    input_audio: InputAudio


#: The content parts a *user* may send. Discriminated on ``type`` so an unknown
#: part names itself in the error instead of failing every branch.
UserContentPart = Annotated[
    TextContentPart | ImageContentPart | AudioContentPart,
    Field(discriminator="type"),
]

#: Roles that are text-only: images and audio are inputs, not instructions.
TextOnlyContent = str | list[TextContentPart]


class FunctionCall(ContractModel):
    """The function a model asked to invoke.

    ``arguments`` is a JSON *string*, not an object. That is OpenAI's wire
    format and it is deliberate: a partially streamed call has no valid JSON
    yet, so the contract cannot promise a parsed object here.
    """

    name: NonEmptyStr
    arguments: str


class ToolCall(ContractModel):
    """One completed tool call emitted by the assistant."""

    id: NonEmptyStr
    type: Literal["function"] = "function"
    function: FunctionCall


class SystemMessage(ContractModel):
    """Instructions that frame the conversation."""

    role: Literal["system"] = "system"
    content: TextOnlyContent
    name: str | None = None


class DeveloperMessage(ContractModel):
    """``system`` under its newer name; same semantics."""

    role: Literal["developer"] = "developer"
    content: TextOnlyContent
    name: str | None = None


class UserMessage(ContractModel):
    """Input from the end user, optionally multimodal."""

    role: Literal["user"] = "user"
    content: str | Annotated[list[UserContentPart], Field(min_length=1)]
    name: str | None = None


class AssistantMessage(ContractModel):
    """A prior turn from the model, replayed as conversation history."""

    role: Literal["assistant"] = "assistant"
    content: TextOnlyContent | None = None
    refusal: str | None = None
    tool_calls: Annotated[list[ToolCall], Field(min_length=1)] | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _require_some_payload(self) -> Self:
        """Reject an assistant turn that says nothing at all.

        An empty assistant message is almost always a serialization bug in the
        caller's history builder, and it costs prompt tokens for no benefit.
        """
        if self.content is None and self.refusal is None and not self.tool_calls:
            raise ValueError(
                "an assistant message must carry at least one of "
                "'content', 'refusal', or 'tool_calls'"
            )
        return self


class ToolMessage(ContractModel):
    """The result of a tool call, fed back to the model.

    ``tool_call_id`` must match an ``id`` from an earlier assistant turn; the
    request-level validator enforces that, since a single message cannot see
    its own history.
    """

    role: Literal["tool"] = "tool"
    content: TextOnlyContent
    tool_call_id: NonEmptyStr


#: Any message a caller may put in ``messages``.
Message = Annotated[
    SystemMessage | DeveloperMessage | UserMessage | AssistantMessage | ToolMessage,
    Field(discriminator="role"),
]
