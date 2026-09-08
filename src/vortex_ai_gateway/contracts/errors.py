"""The error contract, and the translation from pydantic failures into it.

OpenAI clients parse failures out of a single envelope::

    {"error": {"message": ..., "type": ..., "param": ..., "code": ...}}

so the gateway returns exactly that for every failure, whether it originated
here (a malformed request) or upstream (a provider outage). ``param`` is what
makes the envelope useful: it points at the offending field by path, which is
the difference between "something in your request is wrong" and
``messages[2].content``.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, get_args

from pydantic import Field, ValidationError

from vortex_ai_gateway.contracts.base import ContractModel
from vortex_ai_gateway.contracts.messages import Role

#: The failure classes the gateway reports, matching OpenAI's vocabulary so a
#: client's existing error handling keeps working.
ErrorType = Literal[
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "rate_limit_error",
    "api_error",
    "overloaded_error",
]

#: Message roles, which pydantic inserts into an error location as the matched
#: union tag. The caller never sent them as a key, and unlike the content-part
#: and response-format tags they collide with no field name in the contract.
_ROLE_TAGS = frozenset(get_args(Role))

#: Builtin type names pydantic uses to label the branch of a plain union.
_SCALAR_TYPE_LABELS = frozenset(
    {"str", "int", "float", "bool", "bytes", "none", "dict", "list", "tuple", "set", "literal"}
)


def _is_type_label(segment: str) -> bool:
    """Is this location segment a pydantic type label rather than a sent key?

    Every key in this contract is lower snake_case, so a generic parameter
    (``list[tagged-union[...]]``), a builtin name, a wrapper prefix, or a class
    name (``NamedToolChoice``) can only have been synthesised by pydantic.
    """
    return (
        "[" in segment
        or segment in _SCALAR_TYPE_LABELS
        or segment.startswith(("constrained-", "function-"))
        or (segment.isidentifier() and segment[:1].isupper())
    )


#: Location prefixes added by the transport (FastAPI tags request-body errors
#: with ``body``), which mean nothing to a caller reading ``param``.
_TRANSPORT_PREFIXES = frozenset({"body", "query", "path", "header", "cookie"})


class ErrorDetail(ContractModel):
    """The body of an error envelope."""

    message: str = Field(description="Human-readable explanation, safe to show a developer.")
    type: ErrorType = "invalid_request_error"
    param: str | None = Field(
        default=None,
        description="Dotted/indexed path to the offending field, e.g. 'messages[0].content'.",
    )
    code: str | None = Field(
        default=None,
        description="Machine-readable reason, e.g. 'extra_forbidden' or 'model_not_found'.",
    )


class ErrorResponse(ContractModel):
    """The only body the gateway ever returns for a failed request."""

    error: ErrorDetail


def format_parameter_path(location: Sequence[str | int]) -> str | None:
    """Render a pydantic error location as a path a caller can act on.

    ``("body", "messages", 0, "user", "content")`` becomes
    ``"messages[0].content"``. Three kinds of segment are removed because they
    appear in no JSON the caller sent: the transport prefix FastAPI adds, the
    branch labels of a union, and the matched discriminator tag. Where OpenAI
    names a payload field after its own tag (``text`` inside a ``text`` part,
    ``json_schema`` inside a ``json_schema`` format), pydantic emits the pair
    consecutively, so collapsing a repeat of the previous segment keeps the
    field and drops the tag.
    """
    kept: list[str | int] = []
    for index, segment in enumerate(location):
        if isinstance(segment, int):
            kept.append(segment)
            continue
        if index == 0 and segment in _TRANSPORT_PREFIXES:
            continue
        if segment in _ROLE_TAGS or _is_type_label(segment):
            continue
        if kept and kept[-1] == segment:
            continue
        kept.append(segment)

    path = ""
    for segment in kept:
        if isinstance(segment, int):
            path += f"[{segment}]"
        else:
            path = f"{path}.{segment}" if path else segment
    return path or None


def _describe(error: Mapping[str, Any]) -> str:
    """Turn one pydantic error dict into a single readable sentence."""
    path = format_parameter_path(error.get("loc", ()))
    message = str(error.get("msg", "invalid value"))
    return f"{path}: {message}" if path else message


def error_response_from_validation_error(
    exc: ValidationError | Iterable[Mapping[str, Any]],
    *,
    error_type: ErrorType = "invalid_request_error",
) -> ErrorResponse:
    """Build the client-facing envelope for a failed request validation.

    Every failure is reported — a request with three bad fields should take one
    round trip to fix, not three — while ``param`` and ``code`` describe the
    first, since the envelope has room for only one of each.

    Accepts a raw error list as well as a :class:`~pydantic.ValidationError`,
    because FastAPI's ``RequestValidationError`` exposes the same dicts without
    being a pydantic exception.
    """
    raw_errors: list[Mapping[str, Any]] = list(
        exc.errors() if isinstance(exc, ValidationError) else exc
    )
    if not raw_errors:
        return ErrorResponse(error=ErrorDetail(message="Invalid request.", type=error_type))

    first = raw_errors[0]
    return ErrorResponse(
        error=ErrorDetail(
            message="; ".join(_describe(error) for error in raw_errors),
            type=error_type,
            param=format_parameter_path(first.get("loc", ())),
            code=str(first.get("type")) if first.get("type") else None,
        )
    )
