#!/usr/bin/env bash
#
# Live tail of both pipeline functions, interleaved.
#
#   npm run logs              # last 10m, then follow
#   npm run logs -- 1h        # last hour, then follow
#
# Shows the structured JSON lines and errors, dropping Lambda's START/END/REPORT
# noise. Ctrl-C to stop.
#
# ---------------------------------------------------------------------------
# WHY THIS POLLS INSTEAD OF USING `aws logs tail --follow`
#
# `--follow` writes NOTHING when its stdout is a pipe. Verified on aws-cli 2.x /
# macOS: straight to a terminal or a file it streams fine, but through `| grep`
# or `| awk` it produces zero lines indefinitely, and neither `stdbuf -oL` nor
# `PYTHONUNBUFFERED=1` fixes it. Any useful filtering needs a pipe, so --follow
# is unusable here.
#
# This polls with a plain (non-follow) `aws logs tail` and de-duplicates what it
# has already printed. A few more API calls, but it works - and against a
# 60-300s feed cadence, a few seconds of latency is irrelevant.
#
# THREE BUGS NOT TO REINTRODUCE, all of which failed silently:
#   1. The variable was named GROUPS - a bash SPECIAL VARIABLE holding the
#      caller's group IDs. Assignment to it is ignored, so the script read back
#      "20" (the staff gid) and tried to tail a log group named "20".
#   2. The filter was `grep --line-buffered`, a flag BSD/macOS grep REJECTS.
#      With stderr going to /dev/null the whole chain died and printed nothing.
#   3. `describe-log-groups` PAGINATES, and a client-side --query filter runs per
#      page, so pages with no match emitted blank lines into the group list.
# ---------------------------------------------------------------------------

set -uo pipefail
cd "$(dirname "$0")/.."

# npm strips the `--` separator before passing args along. Skip it if it survives.
[ "${1:-}" = "--" ] && shift

SINCE="${1:-10m}"
INTERVAL="${LOGS_POLL_SECONDS:-5}"
REGION_ARG=""
[ -n "${AWS_REGION:-}" ] && REGION_ARG="--region $AWS_REGION"

# --log-group-name-prefix filters SERVER-side and returns a single page (bug 3).
LOG_GROUPS=$(aws logs describe-log-groups $REGION_ARG --log-group-name-prefix CorridorEventHub \
  --no-paginate --query "logGroups[].logGroupName" --output text 2>/dev/null \
  | tr '\t' '\n' | grep -v '^None$' | grep .)

if [ -z "$LOG_GROUPS" ]; then
  echo "no Corridor Event Hub log groups found - is the stack deployed in this region?"
  exit 1
fi

echo "tailing since $SINCE, polling every ${INTERVAL}s (Ctrl-C to stop):"
echo "$LOG_GROUPS" | sed 's/^/  /'
echo

SEEN=$(mktemp)
cleanup() { rm -f "$SEEN"; }
trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT

WINDOW="$SINCE"

while true; do
  for lg in $LOG_GROUPS; do
    case "$lg" in
      *Collector*)  tag="collect" ;;
      *Normalizer*) tag="normal " ;;
      *)            tag="other  " ;;
    esac

    # No --follow: one shot per poll. awk filters AND prefixes - line-buffered
    # via fflush(), and identical on BSD and GNU (bug 2).
    aws logs tail "$lg" $REGION_ARG --since "$WINDOW" --format short 2>/dev/null \
      | awk -v t="$tag" '
          /"msg"|ERROR|errorMessage|Task timed out|WARN/ { print "[" t "] " $0; fflush() }
        '
  done | sort | while IFS= read -r line; do
    # Dedupe across polls so overlapping windows do not reprint lines.
    key=$(printf '%s' "$line" | cksum | tr -d ' ')
    if ! grep -qx "$key" "$SEEN" 2>/dev/null; then
      printf '%s\n' "$key" >> "$SEEN"
      printf '%s\n' "$line"
    fi
  done

  # First pass shows history; later passes only need the poll window, with a
  # little overlap so nothing falls between polls.
  WINDOW="$((INTERVAL * 3))s"
  sleep "$INTERVAL"
done
