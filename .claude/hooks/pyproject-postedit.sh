#!/usr/bin/env bash
# PostToolUse on Write|Edit of pyproject.toml — re-lock and install.
#
# CI installs with `uv sync --locked`, which fails outright if uv.lock is out of
# step with pyproject.toml. A dependency edit without a matching lockfile update
# is therefore a guaranteed red build, discovered minutes later in CI rather
# than seconds later here.
#
# `uv sync` rather than `uv lock`: locking alone updates the lockfile without
# installing anything, so the next `uv run --no-sync pytest` fails on the import
# of a dependency that is now in the lockfile and not in the venv. `uv sync`
# re-locks *and* installs, so it is a strict superset. Note the deliberate
# absence of --locked: this hook exists to move the lockfile, not to assert it
# has not moved.
set -uo pipefail

payload=$(cat)
file=$(printf '%s' "$payload" | jq -r '.tool_response.filePath // .tool_input.file_path // empty')
[[ ${file} == */pyproject.toml || ${file} == pyproject.toml ]] || exit 0

root=$(git -C "$(dirname "${file}")" rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "${root}" || exit 0

if out=$(uv sync 2>&1); then
  if git diff --quiet -- uv.lock 2>/dev/null; then
    exit 0  # nothing moved; the edit did not touch dependencies
  fi
  jq -cn '{systemMessage: "pyproject.toml changed — uv.lock refreshed. Commit both together or CI (uv sync --locked) fails.", suppressOutput: true}'
else
  jq -cn --arg o "${out}" '{systemMessage: ("uv sync FAILED — uv.lock is now out of step with pyproject.toml and CI will fail:\n" + $o)}'
fi
exit 0
