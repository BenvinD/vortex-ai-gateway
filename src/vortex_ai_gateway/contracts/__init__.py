"""The gateway's unified, OpenAI-compatible API contract.

This package is the only place the public wire format is defined. It depends on
nothing else in the codebase — not FastAPI, not the config, not any provider
SDK — so the contract can be imported by routing, adapters, and tests alike
without dragging the application along with it.

Layering::

    base       ContractModel: strictness settings every model inherits
    messages   roles, multimodal content parts, tool calls
    tools      tool declarations and tool-choice control
    options    response format, streaming options, predicted output
    usage      token accounting and log probabilities
    chat       ChatCompletionRequest / Response / Chunk
    catalog    model listing
    errors     the error envelope and the pydantic -> envelope translation

Import from this package rather than the submodules; the split is an
organising device, not a stable path.
"""

from vortex_ai_gateway.contracts.base import ContractModel, NonEmptyStr, UnixTimestamp
from vortex_ai_gateway.contracts.catalog import ModelCard, ModelList
from vortex_ai_gateway.contracts.chat import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChoiceDelta,
    FinishReason,
    FunctionCallDelta,
    GatewayMetadata,
    LogitBias,
    ResponseMessage,
    ServiceTier,
    ToolCallDelta,
)
from vortex_ai_gateway.contracts.errors import (
    ErrorDetail,
    ErrorResponse,
    ErrorType,
    error_response_from_validation_error,
    format_parameter_path,
)
from vortex_ai_gateway.contracts.messages import (
    AssistantMessage,
    AudioContentPart,
    DeveloperMessage,
    FunctionCall,
    ImageContentPart,
    ImageUrl,
    InputAudio,
    Message,
    Role,
    SystemMessage,
    TextContentPart,
    ToolCall,
    ToolMessage,
    UserContentPart,
    UserMessage,
)
from vortex_ai_gateway.contracts.options import (
    JsonObjectResponseFormat,
    JsonSchemaDefinition,
    JsonSchemaResponseFormat,
    PredictedOutput,
    ResponseFormat,
    StreamOptions,
    TextResponseFormat,
)
from vortex_ai_gateway.contracts.tools import (
    FunctionDefinition,
    NamedToolChoice,
    NamedToolFunction,
    ToolChoice,
    ToolChoiceMode,
    ToolDefinition,
)
from vortex_ai_gateway.contracts.usage import (
    ChoiceLogprobs,
    CompletionTokensDetails,
    PromptTokensDetails,
    TokenLogprob,
    TokenUsage,
    TopLogprob,
)

__all__ = [
    "AssistantMessage",
    "AudioContentPart",
    "ChatCompletionChoice",
    "ChatCompletionChunk",
    "ChatCompletionChunkChoice",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChoiceDelta",
    "ChoiceLogprobs",
    "CompletionTokensDetails",
    "ContractModel",
    "DeveloperMessage",
    "ErrorDetail",
    "ErrorResponse",
    "ErrorType",
    "FinishReason",
    "FunctionCall",
    "FunctionCallDelta",
    "FunctionDefinition",
    "GatewayMetadata",
    "ImageContentPart",
    "ImageUrl",
    "InputAudio",
    "JsonObjectResponseFormat",
    "JsonSchemaDefinition",
    "JsonSchemaResponseFormat",
    "LogitBias",
    "Message",
    "ModelCard",
    "ModelList",
    "NamedToolChoice",
    "NamedToolFunction",
    "NonEmptyStr",
    "PredictedOutput",
    "PromptTokensDetails",
    "ResponseFormat",
    "ResponseMessage",
    "Role",
    "ServiceTier",
    "StreamOptions",
    "SystemMessage",
    "TextContentPart",
    "TextResponseFormat",
    "TokenLogprob",
    "TokenUsage",
    "ToolCall",
    "ToolCallDelta",
    "ToolChoice",
    "ToolChoiceMode",
    "ToolDefinition",
    "ToolMessage",
    "TopLogprob",
    "UnixTimestamp",
    "UserContentPart",
    "UserMessage",
    "error_response_from_validation_error",
    "format_parameter_path",
]
