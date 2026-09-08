# ADR-023: Agent tooling is checked in, and mirrors CI rather than replacing it
Date / Status: 2026-09-07 / accepted
Context: The conventions in this repo are enforced by habit: the four CI steps
in order, an ADR's index row, a design note before the code, `uv lock` after a
dependency edit. Each is cheap to do and invisible when skipped — the ADR index
row and the lockfile refresh most of all, because both fail late (an unfindable
decision; a red build). An assistant working here rediscovers all of it from
`CLAUDE.md` every session.
Options: A) leave it to each developer's own `~/.claude` — nothing in the repo,
nothing shared, nothing to review B) commit everything, including model choice
and personal preferences C) commit only the parts that are properties of *this
repository* — hooks that run the tools CI runs, skills that encode the doc
conventions and the provider seam — and leave personal preference to an ignored
`.claude/settings.local.json`.
Decision: C. `.claude/settings.json`, `.claude/hooks/`, `.claude/skills/` and
`.claude/agents/` are committed and reviewed like any other code, because they
encode decisions already recorded in ADRs; `settings.local.json` is git-ignored.
Every hook shells out through `uv run --no-sync` so the ruff and mypy it runs
are the ones CI runs — a hook with its own tool versions is a second source of
truth about whether the code is clean. Three sub-decisions worth recording. The
per-edit hook runs **ruff only**: mypy is given the `src/` package root, and
appending a single file path makes it resolve the same module twice, exactly as
the pre-commit config already documents. The Stop hook runs `ruff check` and
`mypy src` but **not** pytest, because `addopts` carries
`--cov-report=term-missing` and a full coverage table on every stop is pure
context cost; `/verify` runs the tests once, deliberately. And it blocks **at
most once per session** — a Stop hook that blocks on every failure can spin,
being re-invoked to fix something it cannot, so the first block hands the errors
back while they are cheap and after that it reports and lets a human decide.
Consequences: The conventions now fail loudly at the moment they are broken
instead of in review, and `/adr`, `/verify` and `add-provider` mean the
checklists live next to the code rather than in whoever remembers them. The cost
is a second place that names the CI steps: `ci.yml` and the `verify` skill must
be changed together, and a drifted skill is worse than none because it will be
trusted. A hook that needs Redis or a vendor key is deliberately absent — those
belong in CI or in `/verify`, not on every edit. We would revisit A if the
hooks ever became personal preference rather than repo policy, and B never:
model choice is not a property of a repository. The repo-wide guidance itself is
tool-neutral, so it lives in `AGENTS.md` — the convention other coding agents
read — and the root `CLAUDE.md` is a one-line `@AGENTS.md` import plus the
Claude-specific tooling notes. One file, two readers, nothing duplicated and
nothing to keep in step. The *nested* files stay `CLAUDE.md`: Claude Code loads
those on demand when work happens in the directory and does not read `AGENTS.md`
at all, so renaming them would silently drop them. The per-directory `CLAUDE.md`
files are excluded from the wheel in `[tool.hatch.build.targets.wheel]`:
hatchling ships every file under the package directory, so
`providers/CLAUDE.md` was landing in the site-packages of anyone installing the
gateway — instructions for agents editing this tree are not part of what the
gateway distributes. One hook was built and dropped: `uv sync`-on-new-module. `uv sync` installs
this project **editable** — the `.pth` is a bare path to `src/` — so a new
module is importable immediately and the hook was a no-op. The gap it was meant
to close is real but only visible under CI's `--no-editable` wheel install, so
what remains is a warning when a *new top-level package* appears under `src/`,
which hatchling's auto-detection would leave out of the wheel.
