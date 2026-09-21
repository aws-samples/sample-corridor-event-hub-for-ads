#!/usr/bin/env bash
#
# Create the Python venv if it is missing, then install the project into it.
#
#   bash scripts/ensure-venv.sh              # venv + pip install -e '.[dev]'
#   bash scripts/ensure-venv.sh --venv-only  # just the interpreter
#
# This is the npm-script replacement for the Makefile's `install-py` prerequisite.
# make got idempotence for free from the `$(VENV)/bin/activate` file target; npm
# scripts have no dependency graph, so the check lives here and every script that
# needs Python calls this first. Cheap when already installed, which is the common
# case - it is on the front of `npm test`, `npm run probe`, and a dozen others.
#
# Python is pinned to a venv rather than the system interpreter: the Lambda bundle
# needs a modern pip for cross-platform wheels (see scripts/build-lambda.sh), and
# macOS ships pip 21.
#
# The venv is built with the newest interpreter on the box, preferring 3.13 -
# the Lambda runtime (lib/ingest-stack.ts) - so local behaviour matches deployed.
# `python3` is the last resort, not the default: on macOS it is 3.9, which still
# works (pyproject floors there on purpose) but makes boto3 print a
# PythonDeprecationWarning into every process. Override with `PYTHON=... npm run ...`.
#
# The installer directories are searched explicitly for the same reason
# scripts/run-ui.sh appends them for `aws`: an npm- or IDE-launched shell often has
# a narrower PATH than the terminal, so a Homebrew python3.13 is invisible here
# even though it works interactively - and the venv silently lands on 3.9.

set -euo pipefail
cd "$(dirname "$0")/.."

VENV=.venv
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

if [ ! -x "$PY" ]; then
  PYTHON="${PYTHON:-$(
    { for d in "" /opt/homebrew/bin/ /usr/local/bin/; do
        for v in 3.13 3.12 3.11; do command -v "${d}python$v" 2>/dev/null; done
      done; command -v python3; } | head -1
  )}"

  if [ -z "$PYTHON" ]; then
    echo "no python3 found on PATH - install Python 3.11+ and retry" >&2
    exit 1
  fi

  "$PYTHON" -m venv "$VENV"
  "$PIP" install --quiet --upgrade pip
  "$PY" -c 'import sys; v=sys.version_info; print(f"venv python {v.major}.{v.minor}.{v.micro}")'
fi

# `--venv-only` exists so `npm run venv` can hand you an interpreter without the
# dependency install, matching the Makefile's `venv` target.
[ "${1:-}" = "--venv-only" ] && exit 0

# Unconditional, like the phony `install-py` it replaces: pip is the thing that
# knows whether pyproject.toml moved, and it is quiet and fast when it has not.
"$PIP" install --quiet -e '.[dev]'
