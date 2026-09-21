#!/usr/bin/env bash
#
# Run the record lifecycle tracker: the read-only cloud API and its Vite dev server.
#
#   npm run trace-ui
#
# SAME SHAPE AS run-ui.sh, DIFFERENT PORTS - 8788/5174 rather than 8787/5173, so this
# runs alongside the strip UI rather than instead of it. Following a record from the
# corridor view into its history is the normal workflow, and it should not require
# stopping one app to use the other.
#
# The cleanup logic is the same as run-ui.sh's and it is here for the same reason: an
# orphaned API keeps the port, and the next run starts a fresh front end that silently
# talks to a STALE server. In this app that is worse than in the strip - the strip at
# least labels its own freshness, while a stale trace looks like a record that has
# stopped moving, which is a conclusion someone might act on.

set -uo pipefail
cd "$(dirname "$0")/.."

API_PORT="${CEH_TRACE_API_PORT:-8788}"
UI_PORT=5174
PY=.venv/bin/python

# Make the AWS CLI and any credential_process helper findable.
#
# THIS APP CANNOT DEGRADE WITHOUT CREDENTIALS, unlike the strip - which falls back to
# captured payloads and says so. Everything here comes from the deployed stack, so a
# narrower PATH in a make- or IDE-launched shell means an empty tool with a
# credentials error rather than a partial view. Standard installer locations are
# appended, so anything already on PATH still wins.
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

# Kill by PORT, not by remembered pid: `npm run dev` spawns vite as a grandchild and
# the python module leaves a wrapper around the interpreter, so killing the pid we
# started reliably orphans whatever actually holds the socket.
kill_port() {
  local port="$1" pid
  pid="$(port_pid "$port")"
  [ -z "$pid" ] && return 0
  kill "$pid" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.2
    [ -z "$(port_pid "$port")" ] && return 0
  done
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

echo "trace API on :$API_PORT, tracker UI on :$UI_PORT - Ctrl-C stops both"

# Report the credential situation up front. Unlike the strip, there is no degraded
# mode here, so an unset profile is worth naming BEFORE the browser opens on an error.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  echo "note: AWS_PROFILE is unset - the tracker reads the DEPLOYED stack and cannot"
  echo "      fall back to captured payloads. Set it first:"
  echo "        export AWS_PROFILE=<your-profile>"
else
  echo "credentials: ${AWS_PROFILE:-<environment credentials>} in ${AWS_DEFAULT_REGION:-${AWS_REGION:-<region from profile>}}"
fi

# NO `set -m`, deliberately: job control would put each child in its own process
# group, so a terminal Ctrl-C never reaches them and the trap has to do all the work.
"$PY" -m corridor_event_hub.trace_server --port "$API_PORT" &
api_pid=$!

# Fail fast if the API cannot bind - otherwise the UI comes up and every request
# errors, which reads like a front-end bug.
for _ in $(seq 1 40); do
  if ! kill -0 "$api_pid" 2>/dev/null; then
    echo "the trace API exited during startup - run 'npm run trace' to see why" >&2
    exit 1
  fi
  [ -n "$(port_pid "$API_PORT")" ] && break
  sleep 0.25
done

CEH_TRACE_API_PORT="$API_PORT" npm --prefix ui-trace run dev &
ui_pid=$!
wait "$ui_pid"
