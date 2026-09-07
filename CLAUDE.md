@AGENTS.md

## Claude Code

Everything above is tool-neutral and lives in `AGENTS.md`, which other coding
agents read directly. This file adds only what is specific to Claude Code
(ADR-023).

### Use the skills rather than working from memory

Each encodes a checklist that is currently maintained by hand, and each names
the files it has to touch:

| Skill | For |
|---|---|
| `/verify` | the four CI steps, in order, against a `--no-editable` install |
| `/adr` | a new ADR **and** its row in the index table — the row is the half that gets forgotten |
| `/design-note`, `/day-note` | the `docs/design/` and `docs/notes/` formats |
| `add-provider` | the eight files a vendor touches, and the ADR-014/015 translation rules |
| `add-tests` | the right test double per subsystem, and the two bugs that got through 541 passing tests |
| `capture-golden` | replacing a reconstructed fixture with real vendor bytes |

Two review subagents: `adapter-reviewer` (a provider file against the seam
rules) and `adr-auditor` (read-only; ADR files vs. the index, and drift between
`AGENTS.md` and the code).

### Hooks run some of this for you

`.claude/settings.json` wires three scripts in `.claude/hooks/`, all shelling
out through `uv run --no-sync` so the tools are the ones CI runs. On a Python
edit, ruff fixes and formats that file. On a `pyproject.toml` edit, `uv sync`
re-locks and installs, because CI's `uv sync --locked` fails on a stale
lockfile. On stop, `ruff check src tests` and `mypy src` run, blocking at most
once per session.

Do not add mypy to the per-file hook: mypy is given the `src/` package root, and
appending a single path makes it resolve the same module twice.

### Iterating costs context

`addopts` carries `--cov-report=term-missing`, so every plain `pytest` prints
the whole coverage table. Iterate with
`uv run --no-sync pytest -q --no-cov -x tests/test_<file>.py` and let `/verify`
run the coverage-bearing suite once, at the end.

### Detail loads where it applies

`src/vortex_ai_gateway/providers/`, `tests/` and `docs/` each have their own
`CLAUDE.md`, loaded on demand when work happens in that directory rather than
in every session. Keep per-area detail there and this file short. Those stay
`CLAUDE.md` deliberately — Claude Code loads nested `CLAUDE.md` on demand and
does not read `AGENTS.md` at all, so renaming them would silently lose them.
