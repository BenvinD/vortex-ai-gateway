"""Embedders behind the :class:`~vortex_ai_gateway.semantic.Embedder` seam.

The semantic cache (ADR-024) needs a vector for a prompt and does not care
where it comes from; this module is where one comes from. It is deliberately
*not* under ``providers/``: that package is the ``ChatProvider`` seam, two
methods wide on purpose (ADR-011), and an embedder is a different seam with a
different shape — one call, one vector, no streaming, no usage. Sharing the
chat adapter's base class would inherit a status→exception taxonomy built for
a bill-bearing completion call, when the only thing the semantic cache does
with an embedder failure is log one warning and serve uncached.

There is one implementation, against Ollama's ``/api/embed``, because that is
the server ``scripts/threshold_experiment.py`` measured against (ADR-005) and
the threshold only means anything for the model that produced the vectors. A
hosted embedding API would be a second class here with the same two methods.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

import httpx

from vortex_ai_gateway.providers.http import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from vortex_ai_gateway.config import Settings
    from vortex_ai_gateway.semantic import Embedder

#: Where the Ollama chat adapter also defaults to; one server, two endpoints.
DEFAULT_OLLAMA_URL: Final = "http://localhost:11434"

#: The batch endpoint, used for a batch of one. ``/api/embeddings`` (plural,
#: singular ``prompt``) is the deprecated form and is what the model card
#: examples still show; ``/api/embed`` is what the experiment script calls, so
#: a vector from here is comparable with the ones the threshold was tuned on.
EMBED_PATH: Final = "/api/embed"


class EmbeddingError(RuntimeError):
    """The embedder did not produce a vector.

    One class, not a taxonomy: the semantic cache treats every embedder
    failure the same way — a warning and an uncached answer — so the transport
    detail belongs in the message for the log, not in the type.
    """


class OllamaEmbedder:
    """An :class:`~vortex_ai_gateway.semantic.Embedder` over Ollama's ``/api/embed``.

    ``client`` may be injected, in which case it is the caller's and is not
    closed by :meth:`aclose` — the same ownership rule as the chat adapters.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model:
            raise ValueError("an embedding model name is required")
        self.model = model
        self.base_url = (base_url or DEFAULT_OLLAMA_URL).rstrip("/")
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._client = client
        self._owns_client = client is None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r}, base_url={self.base_url!r})"

    @property
    def client(self) -> httpx.AsyncClient:
        """The HTTP client, opened on first use so construction is free."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout)
            )
        return self._client

    async def aclose(self) -> None:
        """Close the client, if this embedder opened it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed(self, text: str) -> Sequence[float]:
        """The vector for ``text``, as the model returns it (not yet normalised).

        Everything that can go wrong — refused connection, timeout, a non-2xx
        status, a 200 whose body is not a vector — surfaces as one
        :class:`EmbeddingError`, because the caller has exactly one response to
        all of them.
        """
        try:
            response = await self.client.post(
                f"{self.base_url}{EMBED_PATH}", json={"model": self.model, "input": text}
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"ollama did not answer: {type(exc).__name__}: {exc}") from exc
        if response.status_code != httpx.codes.OK:
            raise EmbeddingError(
                f"ollama returned {response.status_code}: {response.text[:200] or 'no detail given'}"
            )
        return _vector_from(response, model=self.model)


def _vector_from(response: httpx.Response, *, model: str) -> Sequence[float]:
    """The first row of ``embeddings``, or why there is not one.

    Ollama answers a request for a model it has not pulled with a 404, but a
    model that is loaded and *not* an embedding model answers 200 with an empty
    ``embeddings`` list — so "is there a row" is checked as carefully as "did
    it parse".
    """
    try:
        body: Any = response.json()
    except ValueError as exc:
        raise EmbeddingError(f"ollama returned a body that is not JSON: {exc}") from exc
    rows = body.get("embeddings") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows:
        raise EmbeddingError(
            f"ollama returned no embedding for model {model!r}; is it an embedding model?"
        )
    vector = rows[0]
    if not isinstance(vector, list) or not all(isinstance(x, int | float) for x in vector):
        raise EmbeddingError("ollama returned an embedding that is not a list of numbers")
    return vector


def build_embedder(settings: Settings) -> Embedder | None:
    """The embedder ``settings`` names, or ``None`` when it names none.

    ``embedding_model`` is the whole switch: naming a model is the one decision
    ADR-005 leaves to the deployment, and there is no default because a
    threshold carried across models is a prior again. The server falls back to
    the chat adapter's ``ollama_base_url`` so one Ollama needs configuring once.
    """
    if not settings.embedding_model:
        return None
    return OllamaEmbedder(
        settings.embedding_model,
        base_url=settings.embedding_base_url or settings.ollama_base_url or None,
        timeout=settings.request_timeout_seconds,
    )
