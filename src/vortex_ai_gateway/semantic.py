"""The semantic cache: a question close enough to one already answered.

The exact cache (``cache.py``, ADR-004) answers "have I seen these bytes". This
one answers "have I seen this *question*", and the pipeline is four steps:
embed the prompt, take the cosine similarity against every stored vector in the
same namespace, compare the best one to a threshold, and call it a hit or a
miss. The four steps are the whole design; what follows is which parts of the
request take part in each.

**Only the words are compared; everything else must match exactly.** A prompt
sent to a different model, at a different temperature, with a different tool
list or ``response_format``, is a different question however similar the text.
So the request is split in two: ``messages`` is rendered to text and embedded,
and the rest of the validated request — dumped and hashed the same way the
exact cache does it — becomes part of the namespace the vector is searched in.
Two vectors are never compared across a namespace boundary, which is what makes
a threshold about *wording* and nothing else.

**Cosine similarity is a dot product over unit vectors.** Every vector is
normalised to length one on the way in, once, so a lookup is one matrix-vector
product over the namespace's rows and an ``argmax``. The score that comes back
is ``[-1, 1]``, and it is reported on a miss as well as a hit, because the
nearest miss is the only evidence there is for where the threshold should be
(ADR-005).

**Requests with non-text content are not compared.** A text embedder cannot
see an image, so two requests with the same caption and different pictures
would embed identically and serve each other's answer. They bypass, as
streaming requests do, and are counted as bypasses rather than misses.

**Entries live in this process.** The vectors are a numpy matrix per
namespace, per worker, like the breaker state and the cache counters. That is
where the exact cache's Redis entries are the next step, not this one: the
similarity search needs the rows in memory whichever process owns them.

**Nothing here stops the gateway.** An embedder that fails, a vector that comes
back the wrong shape, a zero vector with no direction — each is one warning,
one error counted, and a miss. The request is served either way.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, Self

import numpy as np
import structlog
from numpy.typing import NDArray

from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.cache import (
    CacheOutcome,
    CacheStats,
    bypass_requested,
    canonical_payload,
)
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest, ChatCompletionResponse
from vortex_ai_gateway.contracts.messages import TextContentPart

logger = structlog.get_logger(__name__)

#: A unit vector, ``float32``. Every vector this module holds or compares.
Vector = NDArray[np.float32]

#: The cosine similarity two prompts must reach to be the same question. High
#: on purpose: a paraphrase of a question scores well above this on any
#: sentence embedder worth using, and a question about a different subject
#: scores well below it. What lies between is exactly what ADR-005 has to
#: measure before choosing a number.
DEFAULT_THRESHOLD: Final = 0.95

#: What the caller is told this tier did, beside the exact cache's ``X-Cache``.
#: Absent when no semantic cache is configured, for the same reason.
SEMANTIC_HEADER: Final = "x-semantic-cache"

#: The best cosine similarity seen, on a hit or a miss, to four places. On a
#: miss it is how close the nearest stored question came — the number the
#: threshold is tuned against (ADR-005). Absent when there was nothing to
#: compare with.
SEMANTIC_SCORE_HEADER: Final = "x-semantic-cache-score"

#: The embedder failed, or gave back something that is not a vector. One
#: warning, as the exact cache warns when Redis is gone, for the same reason.
DEGRADED_EVENT: Final = "semantic cache unavailable; serving uncached"

#: How the message list is rendered before it is embedded. The role is kept:
#: ``user: hello`` and ``assistant: hello`` are different turns, and an embedder
#: given only the words could not tell them apart.
TURN_FORMAT: Final = "{role}: {text}"


class Embedder(Protocol):
    """Whatever turns a prompt into a vector.

    A protocol rather than a base class for the reason ``ChatProvider`` is one
    (ADR-011): the vendor adapters are behind the same kind of seam, and the
    tests drive this one with an embedder that returns what they say it should.
    The vector's dimension is the embedder's business; the index learns it from
    the first vector and rejects any later one that disagrees.
    """

    async def embed(self, text: str) -> Sequence[float]:
        """The vector for ``text``. Need not be normalised; will be."""
        ...


class SemanticCacheConfigError(ValueError):
    """A threshold that cannot be a cosine similarity."""


def unit(vector: Sequence[float] | NDArray[Any]) -> Vector:
    """``vector`` scaled to length one, as ``float32``.

    Normalising on the way in is what turns every later cosine into a dot
    product, and it happens once per vector rather than once per comparison.
    A vector with no length has no direction and cannot be compared with
    anything; it is refused rather than turned into ``nan``, because ``nan``
    compares ``False`` against every threshold and would be a silent miss
    forever.
    """
    array = np.asarray(vector, dtype=np.float32)
    if array.ndim != 1 or array.size == 0:
        raise ValueError(f"expected a one-dimensional vector, got shape {array.shape}")
    norm = float(np.linalg.norm(array))
    if not np.isfinite(norm) or norm == 0.0:
        raise ValueError("vector has no direction (zero or non-finite norm)")
    scaled: Vector = array / norm
    return scaled


def prompt_text(request: ChatCompletionRequest) -> str | None:
    """The conversation as the one string that gets embedded.

    Every turn on its own line with its role in front, so the embedder sees
    the same shape of text however the caller split the content into parts.
    ``None`` means the request holds something a text embedder cannot see —
    an image, an audio clip — and must not be compared at all.
    """
    turns: list[str] = []
    for message in request.messages:
        content = message.content
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        else:
            parts = [part for part in content if isinstance(part, TextContentPart)]
            if len(parts) != len(content):
                return None
            text = "".join(part.text for part in parts)
        turns.append(TURN_FORMAT.format(role=message.role, text=text))
    return "\n".join(turns)


def shape_digest(request: ChatCompletionRequest) -> str:
    """A SHA-256 over everything in ``request`` that is not the words.

    The exact cache's canonical payload with ``messages`` removed: model,
    sampling parameters, tools, response format, ``seed`` and ``n`` — every
    field that decides what the model would generate given the same prompt.
    Two prompts are only ever compared inside one shape, so this is the half
    of the key that says *what kind* of question is being asked.
    """
    payload = canonical_payload(request)
    payload.pop("messages", None)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Match:
    """The nearest stored entry to a query, and how near."""

    score: float
    response: ChatCompletionResponse


class VectorIndex:
    """Unit vectors in a numpy matrix, and the response stored beside each.

    Rows are appended into a preallocated block that doubles when full, so
    a store is amortised O(1) rather than a copy of the whole matrix. A lookup
    is a single ``rows @ query`` over the live rows and an ``argmax``: no
    approximate search, no tree, because the number of distinct questions one
    tenant asks one model at one temperature is thousands, not millions, and
    an exact answer at that size is a few microseconds.
    """

    #: The first allocation, in rows. Small: most namespaces hold a handful.
    INITIAL_CAPACITY: Final = 16

    def __init__(self) -> None:
        self._rows: Vector | None = None
        self._size = 0
        self._responses: list[ChatCompletionResponse] = []

    def __len__(self) -> int:
        return self._size

    @property
    def dimension(self) -> int | None:
        """The embedder's dimension, learned from the first row. ``None`` until then."""
        return None if self._rows is None else int(self._rows.shape[1])

    def add(self, vector: Vector, response: ChatCompletionResponse) -> None:
        """Store ``response`` under ``vector``, which must already be a unit vector."""
        if self._rows is None:
            self._rows = np.empty((self.INITIAL_CAPACITY, vector.shape[0]), dtype=np.float32)
        elif vector.shape[0] != self._rows.shape[1]:
            # A different dimension is a different embedding model, and vectors
            # from two models are not comparable however they line up.
            raise ValueError(
                f"vector has dimension {vector.shape[0]}, index holds {self._rows.shape[1]}"
            )
        if self._size == self._rows.shape[0]:
            grown = np.empty((self._size * 2, self._rows.shape[1]), dtype=np.float32)
            grown[: self._size] = self._rows[: self._size]
            self._rows = grown
        self._rows[self._size] = vector
        self._responses.append(response)
        self._size += 1

    def nearest(self, vector: Vector) -> Match | None:
        """The stored entry with the highest cosine similarity to ``vector``.

        ``None`` for an empty index — there is nothing to be near — rather than
        a match at ``-inf``, so the caller's threshold check is not asked to
        reason about a score that is not one.
        """
        if self._rows is None or self._size == 0:
            return None
        if vector.shape[0] != self._rows.shape[1]:
            raise ValueError(
                f"vector has dimension {vector.shape[0]}, index holds {self._rows.shape[1]}"
            )
        scores = self._rows[: self._size] @ vector
        best = int(np.argmax(scores))
        return Match(score=float(scores[best]), response=self._responses[best])


@dataclass
class SemanticLookup:
    """What the semantic cache found, and what a following store needs.

    Shaped like the exact cache's ``Lookup`` so ``routes.py`` treats the two
    the same way: a lookup that decided the request is not comparable, or found
    a hit, carries no ``index``, and that makes the store a no-op by
    construction. ``score`` is the best similarity seen whether or not it
    cleared the threshold — the number a miss is measured by.
    """

    outcome: CacheOutcome | None = None
    response: ChatCompletionResponse | None = None
    score: float | None = None
    index: VectorIndex | None = field(default=None, repr=False)
    vector: Vector | None = field(default=None, repr=False)

    @property
    def headers(self) -> dict[str, str]:
        """The semantic tier's headers, or nothing when there is no such tier."""
        if self.outcome is None:
            return {}
        headers: dict[str, str] = {SEMANTIC_HEADER: self.outcome}
        if self.score is not None:
            headers[SEMANTIC_SCORE_HEADER] = f"{self.score:.4f}"
        return headers


class SemanticCache:
    """Near-match request→response entries, one vector index per namespace.

    ``embedder`` of ``None`` is a working no-op, as ``redis`` of ``None`` is
    for the exact cache: every lookup reports nothing and every store does
    nothing, and a local run with no embedding model configured needs no
    branch anywhere else.
    """

    def __init__(
        self,
        embedder: Embedder | None,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        scope: str = "key",
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            # A threshold at or below zero accepts orthogonal prompts as the
            # same question; above one nothing can ever hit. Neither is a
            # setting anyone chose on purpose.
            raise SemanticCacheConfigError(
                f"threshold must be in (0, 1], got {threshold!r}",
            )
        self._embedder = embedder
        self.threshold = threshold
        self.scope = scope
        self.stats = CacheStats()
        self._indexes: dict[str, VectorIndex] = {}

    @classmethod
    def from_settings(cls, settings: Settings, embedder: Embedder | None) -> Self:
        """The semantic cache a deployment's configuration describes.

        ``embedder`` is passed in rather than built here: which model embeds is
        the half of ADR-005 the configuration does not yet decide, and the
        exact cache takes its Redis connection the same way. The scope is the
        exact cache's — the argument for not sharing an answer across tenants
        (ADR-004) is the same whichever tier found it.
        """
        return cls(
            embedder if settings.semantic_cache_enabled else None,
            threshold=settings.semantic_cache_threshold,
            scope=settings.cache_scope,
        )

    @property
    def configured(self) -> bool:
        """Whether there is an embedder, and so a tier to report on."""
        return self._embedder is not None

    def skipped(self) -> SemanticLookup:
        """The lookup that did not happen because the exact tier said not to.

        The exact cache's bypasses — a route with a zero TTL, the bypass
        header, a stream — are policy about the *request*, not about one tier,
        so this tier honours them without re-deciding them. Counted as a
        bypass here too, so the two tiers' counters describe the same traffic.
        """
        if self._embedder is None:
            return SemanticLookup()
        self.stats.bypasses += 1
        return SemanticLookup(outcome="BYPASS")

    def namespace_for(self, principal: Principal, request: ChatCompletionRequest, path: str) -> str:
        """Which index ``request`` is searched in: caller, route, and shape.

        Scoped by ``key_id`` for the reason the exact cache is (ADR-004), and
        by the shape digest so the vectors in one index differ only in their
        words.
        """
        tenant = "global" if self.scope == "global" else principal.key_id
        return f"{tenant}:{path}:{shape_digest(request)}"

    def index_for(self, namespace: str) -> VectorIndex:
        """The index for ``namespace``, created empty on first sight."""
        return self._indexes.setdefault(namespace, VectorIndex())

    async def lookup(
        self,
        principal: Principal,
        request: ChatCompletionRequest,
        *,
        path: str,
        headers: Mapping[str, str],
    ) -> SemanticLookup:
        """Embed ``request``, find its nearest neighbour, and decide.

        Bypasses are the same three as the exact cache's, plus one of this
        cache's own: a request whose content a text embedder cannot see. All
        return before the embedder is called, which is the expensive step and
        the one that can fail.
        """
        if self._embedder is None:
            return SemanticLookup()

        text = prompt_text(request)
        if request.stream or text is None or bypass_requested(headers):
            self.stats.bypasses += 1
            return SemanticLookup(outcome="BYPASS")

        try:
            vector = unit(await self._embedder.embed(text))
        except Exception as exc:
            self.stats.errors += 1
            logger.warning(DEGRADED_EVENT, operation="embed", error=str(exc))
            return SemanticLookup(outcome="MISS")

        index = self.index_for(self.namespace_for(principal, request, path))
        try:
            match = index.nearest(vector)
        except ValueError as exc:
            # The embedder changed dimension under a live index. Nothing in it
            # is comparable with this vector, and storing would only add one
            # more row that disagrees with the rest.
            self.stats.errors += 1
            logger.warning(DEGRADED_EVENT, operation="search", error=str(exc))
            return SemanticLookup(outcome="MISS")

        if match is not None and match.score >= self.threshold:
            self.stats.hits += 1
            return SemanticLookup(outcome="HIT", response=match.response, score=match.score)

        self.stats.misses += 1
        return SemanticLookup(
            outcome="MISS",
            score=None if match is None else match.score,
            index=index,
            vector=vector,
        )

    async def store(self, lookup: SemanticLookup, response: ChatCompletionResponse) -> None:
        """Keep ``response`` under the vector ``lookup`` computed.

        A no-op unless the lookup was a real miss: the vector is carried from
        the lookup so the prompt is embedded once per request, not twice, and
        so a store cannot land in a different index than the one searched.
        """
        if lookup.index is None or lookup.vector is None:
            return
        lookup.index.add(lookup.vector, response)
        self.stats.stores += 1
