# docs/ — a deliberate paper trail, not generated output

Loaded when work happens in this directory. Three formats, three purposes, and
each has a skill: `/design-note`, `/adr`, `/day-note`.

## design/day-NN.md — written *before* the code

5–10 lines answering three questions: what am I building, what are the 2–3
alternatives, which did I choose and why. Written first, so *the design exists
outside the code, in your own words, before the implementation makes the
decision feel inevitable.*

Group the alternatives per sub-decision rather than as one flat list — `day-07.md`
has four `Alternatives —` blocks because it made four decisions — and give the
genuinely hard part its own paragraph. Price each rejected option; describe it
as someone who understands why it is attractive.

## adr/NNN-short-title.md — one decision, 6–10 lines

From `000-template.md`. Title states the *decision*, not the topic. This repo
owns the `0xx` range; `1xx` belongs to the separate RAG repo.

**An ADR is not done until its row is in the index table in `adr/README.md`.**
Five columns, and the last — "When the rejected option wins" — is the point:
a nameable trigger condition, not a hedge. If you cannot name what would make
you reverse the decision, it is not understood well enough to record yet.

Rows **004** and **005** are deliberate placeholders for decisions not yet made.
Never reuse those numbers for something else; filling one in place is correct
only if you are making that exact decision.

Say what was deliberately *not* built and why — ADR-020's note on per-hop model
rewriting ("it needs syntax, and this needed shipping") is the most useful line
in it.

## notes/day-NN.md — the retrospective

> The "Broke" line is the one that matters. A prediction that matched reality
> taught you nothing; the gap is the lesson.

Format is in `notes/README.md`. Separate the symptom from the bug — day 07's
lesson was not "a parsing bug" but "the bug was not hard to catch, it was hard
to catch *reliably*". Paste real numbers and real output. Use a table when the
point is that some cases behave differently. Tooling lessons count.

If nothing broke, say so and say what was predicted. Do not invent a failure to
fill the heading, and do not drop the heading.

`notes/` also holds weekly exit quizzes as `weekN-quiz.md`.

## Ordering

Design note → code → ADR per decision that landed → day note. The Stop hook
warns when `src/` changed with nothing under `docs/`; it is advisory, because a
one-line fix owes no design note.
