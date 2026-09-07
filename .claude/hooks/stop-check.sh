#!/usr/bin/env bash
# Stop hook — the two whole-tree checks a per-file hook cannot do, plus a nudge
# about this repo's docs convention.
#
# Deliberately not pytest: the suite carries --cov-report=term-missing via
# addopts, so running it here would dump the full coverage table into context on
# every stop. /verify runs the tests, with coverage, once, on purpose.
#
# Blocks at most ONCE per session. A Stop hook that blocks on every mypy failure
# can spin: Claude is re-invoked, cannot fix it, stops, and is re-invoked again.
# One block hands the errors back while they are still cheap to fix; after that
# the same findings are reported as a message and the human decides.
set -uo pipefail

payload=$(cat)
session=$(printf '%s' "$payload" | jq -r '.session_id // "unknown"')
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "${root}" || exit 0

# Skip entirely when no Python changed — a docs-only session owes nothing to
# ruff or mypy, and mypy --strict is not free.
touched_py=$(git status --porcelain 2>/dev/null | grep -E '\.py$' || true)
[[ -n ${touched_py} ]] || exit 0

problems=""
if ! ruff_out=$(uv run --no-sync ruff check src tests 2>&1); then
  problems+="ruff check src tests:"$'\n'"${ruff_out}"$'\n\n'
fi
if ! mypy_out=$(uv run --no-sync mypy src 2>&1); then
  problems+="mypy src (--strict):"$'\n'"${mypy_out}"$'\n\n'
fi

# docs/ is a deliberate paper trail: a design note before the code, an ADR
# alongside a non-obvious decision. A hook cannot enforce "before", but it can
# notice "never". Advisory on purpose — a one-line fix owes no design note.
docs_note=""
touched_src=$(git status --porcelain 2>/dev/null | grep -E ' src/' || true)
touched_docs=$(git status --porcelain 2>/dev/null | grep -E ' docs/' || true)
if [[ -n ${touched_src} && -z ${touched_docs} ]]; then
  docs_note="src/ changed with nothing under docs/. If this embodies a decision, it wants an ADR (/adr) or a design note (/design-note)."
fi

if [[ -z ${problems} ]]; then
  [[ -n ${docs_note} ]] && jq -cn --arg m "${docs_note}" '{systemMessage: $m, suppressOutput: true}'
  exit 0
fi

sentinel="${TMPDIR:-/tmp}/vortex-stop-block-${session}"
if [[ -e ${sentinel} ]]; then
  jq -cn --arg p "${problems}" --arg d "${docs_note}" \
    '{systemMessage: ("Checks still failing (already reported once this session):\n" + $p + $d)}'
  exit 0
fi
: >"${sentinel}"
jq -cn --arg p "${problems}" --arg d "${docs_note}" \
  '{decision: "block", reason: ("These must pass before this is done — CI runs exactly them:\n\n" + $p + $d)}'
exit 0
