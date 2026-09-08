"""Shared base class and scalar aliases for the public API contract.

Everything a client sends or receives is defined in this package. The models
here are the *unified* contract: the single vocabulary the gateway speaks at
its edge, independent of whichever upstream provider ends up serving the
request. Provider-specific request/response shapes live behind an adapter and
never leak into these types.

The wire format is OpenAI's ``/v1/chat/completions``, so an existing OpenAI
client can point its base URL at this gateway and keep working.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

#: A string that must carry at least one character. Used for identifiers where
#: an empty value is always a caller bug (model names, tool names, call IDs).
NonEmptyStr = Annotated[str, Field(min_length=1)]

#: Seconds since the Unix epoch, as OpenAI reports timestamps.
UnixTimestamp = Annotated[int, Field(ge=0)]


class ContractModel(BaseModel):
    """Base for every model in the public contract.

    ``extra="forbid"`` is the load-bearing setting: a request carrying
    ``temperture`` or ``max_token`` is rejected with a message naming the
    offending key, instead of being silently accepted with the default. The
    cost is that a parameter OpenAI adds tomorrow is a 400 here until it is
    declared — a trade we take deliberately (see ADR-010).

    ``validate_assignment`` keeps the same guarantees for models built field by
    field in code, which is how adapters assemble responses.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        # Fields such as `model` are part of OpenAI's wire format; pydantic's
        # `model_` namespace guard would otherwise warn on them.
        protected_namespaces=(),
    )
