# ADR-024: The semantic tier sits behind the exact cache, compares words only, and promotes its hits
Date / Status: 2026-09-14 / accepted
Context: Answering "have I seen this question" rather than "these bytes"
(ADR-004) needs an embedding before it can look at anything, and an embedding
is a network call and a bill. Where in the request path it runs is a cost
question before it is a hit-rate one, and what it compares decides whether a
threshold can mean anything at all.
Options: A) semantic first, exact behind it B) exact first, semantic only on
an exact miss C) one tier keyed on the vector. For what is compared: embed the
whole canonical request, or embed the words and hash the rest. For storage: a
vector database, or numpy in-process.
Decision: B; words only; numpy. Exact first because a hash is free and an
embedding is not, so the expensive tier is asked only when the cheap one
*looked and missed* — not when it declined to look, since a bypass (a stream,
a zero-TTL route, the header) is a decision about the request that the second
tier does not re-decide. `messages` alone is embedded; `model`, sampling
parameters, tools, `seed`, `n` and the rest are hashed into the namespace the
vector is searched in, so a threshold is about wording and nothing else — the
same prompt at a different temperature is a different question however
similar the text. A semantic hit is written back to the exact tier under the
new request's hash, so the next identical request costs a hash, not an
embedding. Cosine over unit vectors is one matrix-vector product and an
`argmax`; a vector database is a server to run for a question thousands of
rows answer in microseconds. Requests with image or audio parts bypass, since
a text embedder cannot see them and two captions over different pictures would
embed identically. The threshold, the model, and what a false hit costs are
ADR-005's, and that ADR currently says: not yet.
Consequences: Paraphrases are free once one of them has been answered, and
`X-Semantic-Cache` says which tier answered, with the nearest score on misses
too. Vectors live per worker with no expiry, like the breakers and counters;
when the fleet needs one shared index the rows move to Redis and the search is
the same product. Option A wins when paraphrase traffic dominates and the exact
tier's hit rate is noise; option C never, because it forfeits the free tier.
