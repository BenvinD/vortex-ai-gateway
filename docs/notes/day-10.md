## Day 10 — a second cache tier, and the number it was supposed to have
Designed: ADR-024 (placement and matching) and ADR-005 (threshold, model, false-positive policy — the placeholder row, filled in place)

Built:
- `semantic.py` — embed → cosine over unit vectors → threshold → hit/miss, in
  numpy. Only `messages` is embedded; everything else in the request is hashed
  into the namespace the vector is searched in, so the threshold is about
  wording alone. `VectorIndex` is a preallocated `float32` matrix that doubles;
  a lookup is `rows @ query` and an `argmax`.
- Wired in behind the exact cache in `routes.py`: consulted only on an exact
  *miss*, never on an exact bypass; a semantic hit is promoted into the exact
  tier so the next identical request costs a hash, not an embedding. The tier
  reports `X-Semantic-Cache` and `X-Semantic-Cache-Score` — the score on misses
  too — and both land on the access line.
- `scripts/threshold_experiment.py` — 20 paraphrase pairs, 20 *hard* near-miss
  pairs, rendered exactly as the gateway renders a prompt, scored with the same
  cosine. Plots to `docs/notes/threshold-<model>.png`, tabulates false hits and
  false misses per candidate threshold, and picks under one rule: no near miss
  may clear it.

**Broke: predicted a threshold near 0.95 would separate paraphrases from near
misses; observed that no threshold does, for either model tried.**

The prior came from reasoning about what a sentence embedder measures. The
data came from asking one:

| model | paraphrase min / median | near-miss median / **max** | at 0.95: false hits / false misses | safe threshold |
|---|---|---|---|---|
| `nomic-embed-text` | 0.838 / 0.948 | 0.893 / **0.996** | 2/20 / 10/20 | none |
| `mxbai-embed-large` | 0.879 / 0.952 | 0.903 / **0.986** | 1/20 / 8/20 | none |

The worst near miss outscores the *best* paraphrase in one model and every
paraphrase but one in the other. 17 of 20 near misses (nomic) score above the
weakest paraphrase. The pairs at the top of the near-miss list are the whole
lesson:

```
0.996  'Convert 5 miles to kilometres.'  vs  'Convert 5 kilometres to miles.'
0.970  'How long should I boil an egg for a soft yolk?'  vs  '... for a hard yolk?'
0.920  'Summarise the plot of Hamlet ...'  vs  'Summarise the plot of Macbeth ...'
0.916  'What does HTTP status 404 mean?'  vs  'What does HTTP status 403 mean?'
```

Cosine over a sentence embedding measures *what the text is about*. The
miles/kilometres pair is the same bag of words in a different order and scores
higher than any two genuine rephrasings of one question; soft/hard, 404/403,
Hamlet/Macbeth are one token apart and the embedding, correctly, places them
next to each other — they *are* about the same thing. But "about the same
thing" is not "has the same answer", and a cache serves answers. Learned: the
threshold is not the knob. Anywhere it admits a paraphrase it admits a near
miss, because the two populations are not two populations to this measure.
The 0.95 default stays — it is the least-bad point in a table with no good
ones — and `VORTEX_SEMANTIC_CACHE_ENABLED` stays off. The tier ships as
plumbing with a documented reason not to turn it on, which is a better state
than plumbing with a number that looks like a setting.

What would change the answer, in the order I would try them: (1) a cheap
exact-match guard on the things embeddings are blind to — the multiset of
numbers, units and known entities in the two prompts must be identical, which
kills miles/km, 404/403, kilobyte/megabyte and 144/169 outright but not
soft/hard; (2) a cross-encoder verifying the top-1 candidate, which is a
second model call per exact miss and so eats into the saving; (3) a
paraphrase-tuned embedder, which would need re-running the script, and the
script is the point — the number is re-derived, not remembered.

Broke, minor: `choose()` returned `1.001` for the nomic run, a threshold
"just above the worst near miss" computed without checking whether anything
was left above it. A function that picks a number will pick one; the
no-safe-threshold outcome had to be a first-class result, not a large float.

Quiz misses: why a unit-vector dot product *is* the cosine; why `argmax` over a
matrix-vector product is exact search and what changes when it stops being
affordable.

One paragraph I could say in an interview: I built a semantic cache tier behind
an exact one — cheap hash first, embedding only on a miss, semantic hits
promoted into the exact tier — and then measured the threshold instead of
choosing it. Forty prompt pairs through two embedders showed the hard near
misses, the ones where one token or a word order changes the answer, score
*higher* than genuine paraphrases; cosine similarity measures topic, not
equivalence. So the tier is wired, tested and off, with the experiment script
checked in so the next embedder gets the same forty questions.
