---
name: adr
description: Write a new Architecture Decision Record from the template and register it in the ADR index table. Use when a change embodies a non-obvious decision, or when asked to "add an ADR", "record this decision", or "document why we chose X".
---

# /adr — one decision, six to ten lines, plus its index row

Argument: a short title for the decision (e.g. `/adr streaming cache policy`).

An ADR that is not in the index table in `docs/adr/README.md` is an ADR nobody
will find. **Three things change every time and all three are required.**

## 1. Pick the number

```bash
ls docs/adr/ | grep -oE '^[0-9]{3}' | sort -n | tail -1
```

Next number is that **+ 1**. Two rules:

- This repo owns the `0xx` range only. `1xx` belongs to the separate RAG repo.
- **Never reuse 004 or 005.** They are reserved placeholders — present as empty
  rows in the index (streaming cache policy, semantic-cache threshold) because
  those decisions are expected but not yet made. Skipping them is intentional;
  filling one *in place* is correct if you are making that exact decision.

## 2. Write the file

Copy `docs/adr/000-template.md` to `docs/adr/NNN-short-title.md` — kebab-case,
no `ADR` prefix in the filename — and fill in every field:

```
# ADR-NNN: <decision>
Date / Status: YYYY-MM-DD / accepted
Context: <2–3 lines: the forces at play>
Options: A) … B) … C) …
Decision: <what and why in 2 lines>
Consequences: <what gets harder/easier; when we'd revisit>
```

House style, taken from the existing ADRs — match it or the new one reads as
foreign:

- **Title states the decision, not the topic.** `020-config-driven-fallback-chains.md`
  is titled "Fallback is an explicit chain in configuration, not implicit rescue".
- **Options are real, not strawmen.** Each must be something a competent person
  would actually choose. Name the concrete cost that ruled it out.
- **Say what was deliberately *not* built and why.** ADR-020's paragraph on
  per-hop model rewriting ("it needs syntax, and this needed shipping") is the
  most useful thing in it.
- Length is 6–10 lines of substance. Prose, not bullets.

## 3. Add the index row

Append to the table in `docs/adr/README.md`, in number order. Five columns, and
the last one is the point of the whole table:

| # | Decision | Chose | Rejected | When the rejected option wins |

"When the rejected option wins" is a real trigger condition, not a hedge —
compare ADR-002's "Fleet outgrows per-worker state — then Redis, fail-open".
If you cannot name the condition under which you would reverse this, the
decision is not understood well enough to record yet.

## Check before finishing

```bash
ls docs/adr/ | grep -oE '^[0-9]{3}' | sort -n   # numbers, no gaps you did not intend
grep -c '^| [0-9]' docs/adr/README.md            # rows == ADR files (+2 placeholders)
```
