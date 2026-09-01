"""Provider implementations behind the unified contract.

The package holds the :class:`ChatProvider` seam and the implementations that
satisfy it. Only :class:`MockProvider` exists so far; the vendor adapters
(OpenAI, Anthropic, Gemini) join it here, each translating the unified contract
to and from its own wire format without that format escaping the module.
"""

from vortex_ai_gateway.providers.base import ChatProvider
from vortex_ai_gateway.providers.mock import (
    MOCK_CREATED,
    CannedReply,
    MockProvider,
    ScriptedOutcome,
)

__all__ = [
    "MOCK_CREATED",
    "CannedReply",
    "ChatProvider",
    "MockProvider",
    "ScriptedOutcome",
]
