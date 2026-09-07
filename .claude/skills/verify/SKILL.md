---
name: verify
description: Run this repo's full CI sequence locally, in CI's exact order, against a non-editable wheel install. Use before opening a PR, before declaring work done, or whenever asked to "check CI", "run the checks", or "verify the build".
---

# /verify — run what CI runs

`.github/workflows/ci.yml` runs four checks after a locked, non-editable
install. Run them **in this order** and stop at the first failure; a later
check's output is noise once an earlier one is red.

```bash
uv sync --locked --no-editable
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy src
uv run --no-sync pytest -v
```

## Why each flag is not optional

- **`--locked`** fails if `uv.lock` is out of step with `pyproject.toml`. This
  is the check that catches a dependency edit whose lockfile was never
  refreshed.
- **`--no-editable`** installs a built wheel into `site-packages` instead of
  linking `src/`. This is the *only* step that proves the tests run against the
  packaged distribution. Day-to-day `uv sync` is editable — a `.pth` pointing at
  `src/` — so a module missing from the wheel is invisible until this runs.
  Combined with the deliberate absence of `pythonpath` in
  `[tool.pytest.ini_options]`, this is what stops a broken wheel from passing.
- **`--no-sync`** on every `uv run` keeps the environment from re-resolving
  between steps, so all four checks see one environment. CI does the same.

## After it runs — pass *or* fail

```bash
uv sync
```

Always, and say that you did. `uv sync --locked --no-editable` leaves the tree
installed **non-editably**, so source edits stop being picked up. If you stopped
at a failing step and skip this, every later `uv run --no-sync pytest` silently
tests the stale wheel instead of your fix — which is the precise failure the
src/ layout exists to make impossible. Re-sync first, then fix.

## Iterating vs. verifying

While fixing something, do **not** use this skill's pytest line. `addopts` in
`pyproject.toml` carries `--cov-report=term-missing`, so every run prints the
full per-file coverage table. Iterate with:

```bash
uv run --no-sync pytest -q --no-cov -x tests/test_<the_one>.py
```

and save the coverage-bearing `pytest -v` for this skill, once, at the end.
