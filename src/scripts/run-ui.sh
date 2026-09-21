#!/usr/bin/env bash
#
# Run the corridor strip app: the Python API and the Vite dev server, together.
#
#   npm run ui
#
# WHY A SCRIPT AND NOT A MAKE RECIPE: cleaning up two processes reliably needs more
# than a trap, and the failure mode is bad enough to be worth real code. An orphaned
# API keeps port 8787, so the next `npm run ui` starts a fresh front end that silently
# talks to a STALE server - the data looks live and is not, which is the one thing
# this whole view exists to avoid.
#
# Three things this does that an inline recipe got wrong:
#   1. Kills by PROCESS GROUP, not pid. `python -m corridor_event_hub.strip_server` leaves a
#      wrapper around the interpreter; killing the pid reaps the wrapper and orphans
#      the server.
#   2. Keeps the pid in a variable, not a file. A pid file plus a `cd` breaks the
#      trap's relative path, and cleanup fails with `cat: .strip-api.pid: not found`.
#   3. Verifies the port is actually free BEFORE starting, and that it is actually
#      released on the way out - a trap that fired but did not work is invisible
#      otherwise.

set -uo pipefail
cd "$(dirname "$0")/.."

API_PORT="${CEH_API_PORT:-8787}"
UI_PORT=5173
PY=.venv/bin/python

# Make the AWS CLI findable.
#
# Feed keys resolve from Secrets Manager by shelling out to `aws` (ADR 0004), and a
# make- or IDE-launched shell frequently has a narrower PATH than the terminal you
# tested in - so `aws` is missing here even though it works interactively. The
# symptom is two sources silently dropping to captured payloads, which is the exact
# "looks live, is not" failure this app exists to prevent.
#
# The credential_process helper matters as much as the CLI itself: a profile using
# `credential_process = <helper> ...` makes the CLI shell out to THAT. If the helper is
# missing, `aws` runs fine and fails with `[Errno 2] No such file or directory:
# '<helper>'` - which reads like a missing AWS CLI and is not. If your profile uses one,
# add its directory to the list below.
#
# Standard installer locations; appended, so anything already on PATH still wins.
for candidate in /usr/local/bin /opt/homebrew/bin "$HOME/.local/bin" "$HOME/.aws/bin"; do
  case ":$PATH:" in
    *":$candidate:"*) ;;
    *) [ -d "$candidate" ] && PATH="$PATH:$candidate" ;;
  esac
done
export PATH

port_pid() { lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1; }

for port in "$API_PORT" "$UI_PORT"; do
  pid="$(port_pid "$port")"
  if [ -n "$pid" ]; then
    cat >&2 <<EOF
Port $port is already in use by pid $pid:

  $(ps -o command= -p "$pid" 2>/dev/null | cut -c1-100)

Refusing to start: a second front end talking to a stale API is worse than a
clear failure. Stop it first, then re-run:

  kill $pid
EOF
    exit 1
  fi
done

api_pid=""
ui_pid=""

# Kill by PORT, not by remembered pid.
#
# This is the part that took several tries. `npm run dev` spawns vite as a
# grandchild and does not forward signals to it, and the python module leaves a
# wrapper around the interpreter - so "kill the pid I started" reliably orphans the
# thing actually holding the socket. Whoever owns the port is definitionally the
# process that has to die, so ask the OS.
kill_port() {
  local port="$1" pid
  pid="$(port_pid "$port")"
  [ -z "$pid" ] && return 0
  kill "$pid" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.2
    [ -z "$(port_pid "$port")" ] && return 0
  done
  # Refused to go on SIGTERM; it is a dev server, not a database.
  kill -9 "$pid" 2>/dev/null
  sleep 0.3
}

cleanup() {
  trap - EXIT INT TERM
  [ -n "$ui_pid" ] && kill "$ui_pid" 2>/dev/null
  [ -n "$api_pid" ] && kill "$api_pid" 2>/dev/null
  kill_port "$UI_PORT"
  kill_port "$API_PORT"

  local left_api left_ui
  left_api="$(port_pid "$API_PORT")"
  left_ui="$(port_pid "$UI_PORT")"
  if [ -n "$left_api$left_ui" ]; then
    echo >&2
    echo "warning: a process survived cleanup:" >&2
    [ -n "$left_api" ] && echo "  :$API_PORT pid $left_api" >&2
    [ -n "$left_ui" ] && echo "  :$UI_PORT pid $left_ui" >&2
    echo "  kill $left_api $left_ui" >&2
  else
    echo "both processes stopped."
  fi
}
trap cleanup EXIT INT TERM

echo "strip API on :$API_PORT, UI on :$UI_PORT - Ctrl-C stops both"

# Report the credential situation UP FRONT rather than letting it show up as two
# `fixture` rows that a reader has to notice and interpret.
if command -v aws >/dev/null 2>&1; then
  # ACCOUNT and REGION, not just "credentials found". Secrets resolve by NAME, so a
  # perfectly valid profile pointed at the wrong account or region fails with a
  # ResourceNotFound that reads like a missing secret - seeing the identity here is
  # what makes that diagnosable. An unresolved account means the credentials are
  # expired or bad, which the profile name alone never shows.
  aws_account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
  aws_region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null)}}"
  echo "aws CLI found - account ${aws_account:-unresolved}, region ${aws_region:-unset}, profile ${AWS_PROFILE:-<default>}"
  if [ -n "${AWS_PROFILE:-}" ]; then
    echo "  keyed feeds will resolve from Secrets Manager"
  else
    echo "  AWS_PROFILE is unset - keyed feeds fall back to captured payloads."
    echo "  export AWS_PROFILE=<profile>   (or TX_DOT_KEY / AZ511_KEY directly)"
  fi
else
  echo "note: no \`aws\` CLI on PATH - TxDOT and AZ511 will use captured payloads."
  echo "  export TX_DOT_KEY=... and AZ511_KEY=... to use live feeds instead."
fi

# NO `set -m` here, deliberately. Job control puts each child in its OWN process
# group, which means a terminal Ctrl-C - delivered to the foreground group - never
# reaches them, and the trap has to do all the work while the user believes the
# signal did it. Without job control the children share this script's group and get
# the SIGINT directly; the trap is then a backstop rather than the only mechanism.
"$PY" -m corridor_event_hub.strip_server --port "$API_PORT" &
api_pid=$!

# Fail fast if the API cannot start - otherwise the UI comes up and every request
# 500s, which looks like a front-end bug.
for _ in $(seq 1 40); do
  if ! kill -0 "$api_pid" 2>/dev/null; then
    echo "the strip API exited during startup - run 'npm run serve' to see why" >&2
    exit 1
  fi
  [ -n "$(port_pid "$API_PORT")" ] && break
  sleep 0.25
done

# Backgrounded and waited on, rather than exec'd, so a Ctrl-C runs the trap instead
# of replacing this shell with npm - which would take the cleanup logic with it.
CEH_API_PORT="$API_PORT" npm --prefix ui run dev &
ui_pid=$!
wait "$ui_pid"

