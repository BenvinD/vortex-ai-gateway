#!/usr/bin/env bash
# PostToolUse on Write|Edit — lint and format the single file that changed.
#
# ruff only, never mypy. mypy is handed the src/ package root (see the comment
# on the mypy hook in .pre-commit-config.yaml); appending an individual file
# path makes it resolve the same module twice and fail with "Duplicate module
# named ...". Whole-tree mypy belongs in the Stop hook, and is there.
#
# Runs through `uv run --no-sync` so the ruff doing the formatting is the same
# ruff CI runs, and so the hook never re-resolves the environment mid-session.
set -uo pipefail

payload=$(cat)
file=$(printf '%s' "$payload" | jq -r '.tool_response.filePath // .tool_input.file_path // empty')
[[ -n ${file} && ${file} == *.py && -f ${file} ]] || exit 0

root=$(git -C "$(dirname "${file}")" rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "${root}" || exit 0

uv run --no-sync ruff check --fix "${file}" >/dev/null 2>&1
uv run --no-sync ruff format "${file}" >/dev/null 2>&1

# The src/ layout means tests import the *installed* distribution. A new module
# inside the existing package needs nothing — the editable install is a .pth
# pointing at src/, so it is importable the moment it is written. A new
# *top-level* package under src/ is different: hatchling auto-detects
# src/vortex_ai_gateway and nothing else, so a second one is absent from the
# wheel CI builds with --no-editable, and passes locally while failing there.
rel=${file#"${root}"/}
if [[ ${rel} == src/* ]]; then
  top=${rel#src/}
  top=${top%%/*}
  if [[ ${top} != "vortex_ai_gateway" && ${top} != *.py ]]; then
    jq -cn --arg t "${top}" '{
      systemMessage: ("New top-level package src/\($t)/ — hatchling only auto-detects src/vortex_ai_gateway, so this will be missing from the wheel CI builds with --no-editable. Add it to pyproject.toml, then run /verify."),
      suppressOutput: true
    }'
  fi
fi
exit 0
