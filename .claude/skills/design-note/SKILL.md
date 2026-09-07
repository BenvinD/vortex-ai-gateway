---
name: design-note
description: Scaffold the next docs/design/day-NN.md before any code is written for it. Use when starting a new day's work or a new subsystem, or when asked to "write the design note", "plan today's work", or "what are we building".
---

# /design-note — written before the code exists

Argument (optional): the day's topic. Omit it and infer from what is being
asked for.

## Next day number

```bash
ls docs/design/ | grep -oE 'day-[0-9]{2}' | sort | tail -1
```

Add one. Design notes and daily notes share the numbering, but the two
directories are **not** required to be in step — `docs/design/day-06.md` exists
with no matching note. Number from `docs/design/` for this skill.

## The format

Per `docs/design/README.md`: written **before** any code is generated, one file
per working day, 5–10 lines, answering three questions.

```markdown
## Day NN — <topic, as a phrase not a noun>

**Building.** <What exists now, what gap that leaves, and what this closes.
Start from the current shortcoming, not from the feature name.>

**Alternatives — <the first sub-decision>.** (1) … (2) … (3) …
Chose (N) — see ADR-NNN. <The interesting part of the decision.>

**Alternatives — <the second sub-decision>.** (1) … (2) … (3) …
Chose (N): <why>.

**<The awkward part.>** <The thing that is genuinely hard here, named as a
heading of its own, with the two or three ways out and which was taken.>
```

## What makes these notes worth writing

Read `docs/design/day-07.md` before writing a new one. Three habits to copy:

- **Each alternative is priced, not dismissed.** "Postgres: the right answer for
  a fleet, and a server to run, a driver to add, and a migration tool to pick
  before a single key exists." The rejected option is described by someone who
  understands why it is attractive.
- **Group alternatives per sub-decision, not one flat list.** Day 07 has four
  separate `Alternatives —` blocks (key storage, where keys are minted, the
  limiter, the estimate) because it made four decisions.
- **Give the hard part its own paragraph.** Day 07's "The awkward part: TPM is a
  limit on something not yet known" is the note's centre of gravity. If nothing
  in the note is awkward, the design has not been thought about yet.

The point, per the README: *the design exists outside the code, in your own
words, before the implementation makes the decision feel inevitable.* So write
this **first**, then the code, then `/adr` for each decision that landed, then
`/day-note` at the end of the day.
