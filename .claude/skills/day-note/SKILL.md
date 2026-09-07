---
name: day-note
description: Scaffold or fill in the next docs/notes/day-NN.md retrospective, whose "Broke" section is the point. Use at the end of a work session, or when asked to "write up the day", "add the daily note", or "record what broke".
---

# /day-note — the retrospective, and the "Broke" line is the point

Argument (optional): the day number or topic. Omit it and infer.

## Next day number

```bash
ls docs/notes/ | grep -oE 'day-[0-9]{2}' | sort | tail -1
ls docs/design/ | grep -oE 'day-[0-9]{2}' | sort | tail -1   # a note usually follows its design note
```

The two sequences are not guaranteed to agree — a design note can exist with no
matching daily note. Pair the note with the day whose work it describes, not
with the highest number present.

## The template

From `docs/notes/README.md`:

```markdown
## Day NN — <topic>
Designed: [day-NN design note](../design/day-NN.md)

Built:
- <module> — <what it does and the one decision inside it> (ADR-NNN).

**Broke: predicted <X>, observed <Y>.** <What happened, why the prediction was
wrong, and what the failure looked like from outside.>

Learned: <the transferable rule>

**One paragraph I could say in an interview.** <Write it.>
```

## The part that carries the weight

> The "Broke" line is the one that matters. A prediction that matched reality
> taught you nothing; the gap is the lesson.

Read `docs/notes/day-07.md` before writing one. What makes it good:

- **The symptom is separated from the bug.** The `parse_key_id` failure was not
  "a parsing bug" — it was a bug that failed 48.3% of the time, which "in a CI
  log looks like flakiness and gets re-run rather than read". That gap *is* the
  lesson: "the bug was not hard to catch, it was hard to catch *reliably*."
- **A table where a table earns it.** The three-ending stream table (completed /
  failed / **abandoned**) makes the invisible failure visible in three rows.
  Use one when the point is that some cases behave differently.
- **Numbers, real ones.** "200 concurrent admissions against a bucket of 50:
  exactly 50 admitted, 150 refused, and the bucket read back at 0.0 rather than
  merely near it." Paste the actual output.
- **Tooling lessons count.** The `types-redis` removal is in day 07 because it
  is the same class of mistake as a pre-emptive `ignore_missing_imports` — "the
  same mistake with worse symptoms, because it fails by being confidently wrong
  rather than by doing nothing."

Write the "Broke" section from what actually happened in the session. If nothing
broke, say so plainly and say what was predicted — do not invent a failure to
fill the heading, and do not omit the heading.

## Related

`docs/notes/` also holds weekly exit quizzes as `weekN-quiz.md`. This skill does
not scaffold those.
