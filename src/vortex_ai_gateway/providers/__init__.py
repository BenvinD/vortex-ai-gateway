"""Provider implementations behind the unified contract.

The package holds the :class:`ChatProvider` seam and the implementations that
satisfy it: :class:`MockProvider` for tests and local development, and one
adapter per vendor. Each adapter translates the unified contract to and from
its own wire format without that format escaping the module — the gateway
above them speaks OpenAI's vocabulary and nothing else (ADR-009, ADR-014).

:class:`HttpChatAdapter` is shared plumbing, not the seam: adapters are
structurally typed against :class:`ChatProvider`, so an implementation owes it
nothing but the two methods.
"""

from vortex_ai_gateway.providers.anthropic import AnthropicAdapter
from vortex_ai_gateway.providers.base import ChatProvider
from vortex_ai_gateway.providers.errors import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    TranslationError,
    UnsupportedParameterError,
)
from vortex_ai_gateway.providers.http import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    HttpChatAdapter,
)
from vortex_ai_gateway.providers.mock import (
    MOCK_CREATED,
    CannedReply,
    MockProvider,
    ScriptedOutcome,
)
from vortex_ai_gateway.providers.ollama import OllamaAdapter
from vortex_ai_gateway.providers.openai import OpenAIAdapter

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MOCK_CREATED",
    "AnthropicAdapter",
    "CannedReply",
    "ChatProvider",
    "HttpChatAdapter",
    "MockProvider",
    "OllamaAdapter",
    "OpenAIAdapter",
    "ProviderAuthError",
    "ProviderBadRequest",
    "ProviderError",
    "ProviderProtocolError",
    "ProviderRateLimited",
    "ProviderTimeout",
    "ProviderUnavailable",
    "ScriptedOutcome",
    "TranslationError",
    "UnsupportedParameterError",
]
