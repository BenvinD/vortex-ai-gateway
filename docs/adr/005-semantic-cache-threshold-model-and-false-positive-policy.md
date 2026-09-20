# ADR-005: A semantic hit must never be a wrong answer; the threshold is measured per model, and no model has passed yet
Date / Status: 2026-09-14 / accepted
Context: The semantic tier (ADR-024) is the first subsystem that can serve a
*wrong* answer — the exact cache can only ever return what the caller asked
for. Whether a stored answer is close enough is one number, the cosine
threshold, and that number only means anything for the model that produced
the vectors. So three things have to be decided together: how much false
hitting is acceptable, how the threshold is chosen, and which model it is
chosen for. Forty prompt pairs were scored to find out (`docs/notes/day-10.md`).
Options: For false positives: A) cost-weighted — accept some wrong answers for
a better hit rate, tuned by a ratio B) zero false hits against a checked-in set
of hard near misses C) a second check on the candidate (numbers/units/entities
must match exactly, or a cross-encoder) before it is served. For the
threshold: A) a fixed prior, 0.95 B) derived by `scripts/threshold_experiment.py`
under the false-positive policy, per model C) tunable per tenant or route. For
the model: A) `nomic-embed-text`, local B) `mxbai-embed-large`, local C) a
hosted embedding API D) none until one passes.
Decision: B, B, D. A cache that is occasionally wrong is worse than no cache,
because the caller cannot tell which answers to distrust — so the policy is
that no pair in the near-miss set may clear the threshold, and the set is
built from the failures that matter: one changed number, a swapped unit, an
antonym. A cost ratio would be a number chosen to make the table come out the
way one hoped. Under that policy the threshold is an *output* of the script,
not an input: the configured `0.95` is a placeholder, valid only for a model
the script has passed, and the script currently passes neither local model —
with both, hard near misses score *above* genuine paraphrases (`5 miles to km`
vs `5 km to miles` at 0.996), so no threshold serves a paraphrase without
serving a wrong answer. Hence no default model and the tier off by default;
the embedder is injected so trying the next one is a script run, not a code
change. Option C for false positives was not built yet: the exact guard on
numbers, units and entities is the likely next step and cheap, but it would
be built on the evidence of a second experiment run, not assumed.
Consequences: The semantic tier ships as tested plumbing with a documented
reason not to enable it, which is a better state than a number that looks like
a setting. Enabling it is a three-line procedure: pull a model, run the script,
set the threshold it prints — and re-run on every model change, because a
threshold carried across models is a prior again. The forty pairs are the
policy's test suite and grow when a new failure class is found. The rejected
cost-weighted option wins where a wrong answer is cheap and observable — a
suggestion, a draft, something a person reviews before it is used — and never
where the completion is the product.
