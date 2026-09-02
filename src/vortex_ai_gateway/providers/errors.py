"""What an adapter raises, classified by what the caller should do about it.

A provider never returns an error envelope — building one is the routing
layer's job (ADR-011). What it *does* owe its caller is a failure that can be
acted on **without reading the message**, because the retry policy, the circuit
breaker and the HTTP status all have to branch on it.

The taxonomy is therefore organised around one question — *is trying again
plausible?* — answered by :attr:`ProviderError.retryable`:

===========================  =========  ===================================
Error                        Retryable  Raised for
===========================  =========  ===================================
:class:`ProviderTimeout`     yes        the answer did not arrive in time
:class:`ProviderRateLimited` yes        ``429``; ``retry_after`` when given
:class:`ProviderUnavailable` yes        unreachable, or ``5xx``
:class:`ProviderBadRequest`  no         ``4xx``, or a request we cannot send
:class:`ProviderAuthError`   no         ``401``/``403`` — *our* credentials
:class:`ProviderProtocolError` no       an answer we cannot read
===========================  =========  ===================================

The four in the middle of that table are the ones a retry loop branches on.
The last two exist because folding them in would make a *lie*: a ``401`` is a
deployment misconfiguration, not the caller's bad request, and must not be
reported to the caller as though their own key were wrong; an unreadable body
usually means the contract has drifted, and retrying reproduces it exactly.

``retryable`` is a class attribute rather than a check the caller writes,
because "which statuses are worth retrying?" is knowledge that belongs next to
the classification, not copied into every retry site.
"""

from typing import ClassVar


class ProviderError(Exception):
    """Base for every failure an adapter reports."""

    #: Whether the same request, sent again, could plausibly succeed. Consulted
    #: by the retry policy (ADR-001) and the circuit breaker (ADR-002).
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        #: Upstream HTTP status, when the failure had one.
        self.status_code = status_code
        #: The vendor's own error code, when it named one.
        self.code = code


class ProviderBadRequest(ProviderError):
    """The provider rejected the request, and would reject it again.

    Also the parent of the failures we raise *before* sending anything, since
    a request that cannot be expressed is a bad request the gateway caught
    first.
    """


class TranslationError(ProviderBadRequest):
    """The request cannot be expressed in this provider's dialect."""


class UnsupportedParameterError(TranslationError):
    """The caller set a parameter this provider has no equivalent for.

    Raised rather than dropped. A gateway that silently ignores ``seed`` or
    ``n`` answers a question the caller did not ask, and the caller has no way
    to find out — the response looks perfectly well-formed.
    """

    def __init__(self, parameter: str, *, provider: str, detail: str | None = None) -> None:
        explanation = detail or f"{provider} has no equivalent"
        super().__init__(
            f"{parameter!r} is not supported by the {provider} provider: {explanation}",
            provider=provider,
            code="unsupported_parameter",
        )
        #: The offending field, named as the caller spelled it.
        self.parameter = parameter


class ProviderAuthError(ProviderError):
    """The provider refused *our* credentials.

    Never the caller's fault, and never retryable: the key is missing, revoked,
    or scoped wrongly, and only a deployment change fixes it.
    """


class ProviderRateLimited(ProviderError):
    """The provider is throttling us."""

    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code, code=code)
        #: Seconds to wait, when the provider said. A retry policy that ignores
        #: this in favour of its own backoff will be throttled again.
        self.retry_after = retry_after


class ProviderTimeout(ProviderError):
    """The answer did not arrive in time.

    Covers every phase httpx distinguishes — connect, read, write and waiting
    for a pooled connection — because from here they mean the same thing, and
    the phase that actually fired is in the message for the log to keep.
    """

    retryable: ClassVar[bool] = True


class ProviderUnavailable(ProviderError):
    """The provider could not be reached, or answered that it is broken."""

    retryable: ClassVar[bool] = True


class ProviderProtocolError(ProviderError):
    """The provider replied with something this adapter cannot read.

    Not retryable: the usual cause is the contract having drifted from the
    vendor's wire format, and the same request reproduces it exactly. The fix
    belongs in ``contracts/``.
    """
