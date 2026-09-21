## Day 11 — the semantic tier gets an embedder, and the checkpoint passes for the wrong reason too
Designed: no design note — this was the step `day-10.md` already named
("pull a model, run the script, set the threshold it prints"), done because a
checkpoint asked for a live semantic HIT and the tier had a protocol with
nothing behind it. ADR-025 records the one decision it embodied.

Built:
- `embedding.py` — `OllamaEmbedder` over `/api/embed`, the same endpoint the
  experiment script calls so its vectors are comparable with the ones the
  threshold was measured on; every failure is one `EmbeddingError`, because the
  semantic tier's response to all of them is one warning (ADR-025).
- `build_embedder(settings)` — `VORTEX_EMBEDDING_MODEL` is the whole switch;
  no default model, URL inherited from `ollama_base_url`. `create_app` builds
  it only when the tier is on and closes it on shutdown; an injected embedder
  is the caller's.
- `tests/test_embedding.py` — 23 tests through `respx`: what is posted, ten
  distinct failures collapsing to one error, client ownership, and the
  cross-product that matters — tier *on*, embedder *real*, server *absent* —
  which serves a `MISS` with `DEGRADED_EVENT` and not a 500.
- The README got the plots and the enable procedure; `.env.example` got the
  four semantic knobs it was missing.

**Broke: predicted the checkpoint could not pass without amending ADR-005;
observed it passes as written, and so does the failure ADR-005 exists for, on
the same server, two requests apart.** The checkpoint said "capital of France"
then "capital city of France" → semantic HIT. It went through at 0.9658 on
`nomic-embed-text`, promoted into the exact tier, third request a 3.5 ms hash
hit. Then, unprompted by the checkpoint:

```
Convert 5 miles to kilometres.     X-Semantic-Cache: MISS  score 0.5270
Convert 5 kilometres to miles.     X-Semantic-Cache: HIT   score 0.9955
What is the capital of Finland?    X-Semantic-Cache: MISS  score 0.8109
```

The km→miles request was served the miles→km answer. Day 10's table said this
would happen (0.996, the top of the near-miss list); seeing it as a response
header on a live gateway is different from seeing it as a row. The prediction
was wrong about *what a passing checkpoint means*: a demo that shows one
paraphrase hitting is evidence the plumbing works, not evidence the threshold
does — and the same three-line procedure that produces the hit produces the
wrong answer. So the tier stays off, ADR-005 is unchanged, and the README shows
both results side by side, because a reader who sees only the HIT will turn it
on.

Broke, minor: the first attempt to background uvicorn from one shell line
`A=… && redis-server … && nohup uvicorn … & sleep 4; curl …` put the *entire*
and-list in the background subshell, including the variable assignment, so the
foreground `tail $A/gateway.log` read `/gateway.log`. The server was running
the whole time; the second launch then failed to bind. One process per line.

Learned: a checkpoint that names one input is a smoke test, and the near-miss
set is the actual eval. Run both, every time, and print them together —
the "it works" example and its nearest "it is wrong" example — or the
demonstration argues for a default the data argued against. Also: the seam an
adapter lives behind should be chosen by what its caller does with failures,
not by what protocol it speaks. The chat adapters' error taxonomy exists
because a wrapper retries on `retryable`; nothing retries an embedding, so the
taxonomy would have been ceremony, and `embedding.py` has one error class.

**One paragraph I could say in an interview.** I put a real embedder behind
the semantic cache's protocol — its own module, one error class, switched on by
naming a model — and ran the checkpoint that had been waiting on it: a
paraphrase hit at 0.966, promoted into the exact tier so the repeat cost a
hash. Then I ran the near miss the threshold experiment had flagged, on the
same server, and got a confidently wrong answer at 0.996. Both went into the
README together. The plumbing is done and the tier is still off, because the
demo that makes it look ready is the same demo that shows why it is not; what
would change that is a numbers-and-units guard in front of the cosine, and a
re-run of the forty pairs.
