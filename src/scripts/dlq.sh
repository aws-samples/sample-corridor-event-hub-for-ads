#!/usr/bin/env bash
#
# Inspect and drain the dead-letter queues.
#
#   ./scripts/dlq.sh              # depth of both queues + a peek at what is in them
#   ./scripts/dlq.sh --peek 10    # show up to N messages in detail
#   ./scripts/dlq.sh --replay     # re-drive normalizer failures, then delete them
#   ./scripts/dlq.sh --purge      # discard everything (asks first)
#
# WHY A DLQ NEEDS A TOOL: a queue nobody can read is only marginally better than a
# dropped message. An unmappable payload goes to a review queue rather than being
# discarded, and the whole point is that a human can then go and LOOK.
# Reading SQS by hand means base64, receipt handles, and visibility timeouts - so
# this wraps it.
#
# THE REPLAY PATH IS THE INTERESTING ONE. Every dead letter carries the original
# EventBridge detail, which carries `bucket` and `key`. The raw bytes are
# still in S3 under Object Lock, so a failure is REPLAYABLE: fix the adapter, run
# --replay, and the same payload is re-normalized from the original bytes. That is
# replay determinism in operational form rather than as a claim in a document.
#
# Every ARN and URL is DISCOVERED, never hardcoded - this stack is deployed to more
# than one account, and a pasted URL fails in a way that looks like an empty queue.
#
# Honours AWS_PROFILE and AWS_DEFAULT_REGION.

set -uo pipefail
cd "$(dirname "$0")/.."

STACK="${STACK:-CorridorEventHubIngest}"
MODE="status"
PEEK=5

# The venv interpreter, so `corridor_event_hub.dlq_format` is importable. Falls back to the
# system python3 with the checkout root on the path, for a machine with no venv yet.
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
  export PYTHONPATH=".:${PYTHONPATH:-}"
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --peek)   MODE="peek"; [ $# -ge 2 ] && [ "${2#-}" = "$2" ] && { PEEK="$2"; shift; } ;;
    --replay) MODE="replay" ;;
    --purge)  MODE="purge" ;;
    --help|-h)
      sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown argument: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "$ACCOUNT" ]; then
  echo "no AWS credentials. Set AWS_PROFILE, or refresh your SSO session."
  exit 1
fi

# --- discover ---------------------------------------------------------------
# From stack OUTPUTS rather than by guessing queue names: the physical names carry
# the stack name, which changes with the -c prefix context value.
#
# TWO SEPARATE QUERIES ON PURPOSE. A single JMESPath joining two pipe expressions
# with a comma is a parse error, and `--query` failures print to stderr while the
# exit code still looks survivable inside `$( )` - so the first version of this
# read an empty string and reported "the stack predates the DLQs" about a stack
# that had them. A wrong "not deployed" message is worse than a crash.
output_value() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
    --output text 2>/dev/null
}

NORM_URL=$(output_value NormalizerDlqUrl)
RULE_URL=$(output_value RuleDlqUrl)
# The resolver's pair. Read with `|| true` semantics via output_value's own 2>/dev/null:
# a stack deployed before the resolver existed has no such outputs, and reporting
# that as a broken account would be the wrong "not deployed" message again.
RESOLVER_URL=$(output_value ResolverDlqUrl)
RESOLVER_RULE_URL=$(output_value ResolverRuleDlqUrl)

if [ -z "${NORM_URL:-}" ] || [ "$NORM_URL" = "None" ]; then
  cat <<EOF
could not find DLQ outputs on stack $STACK.

  account: $ACCOUNT
  region : ${AWS_DEFAULT_REGION:-${AWS_REGION:-<unset>}}
  profile: ${AWS_PROFILE:-<default>}

Either the stack predates the DLQs or the shell points at the wrong account.
Redeploy with:  npm run deploy
EOF
  exit 1
fi

depth() {
  aws sqs get-queue-attributes --queue-url "$1" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --query 'join(`/`, [Attributes.ApproximateNumberOfMessages, Attributes.ApproximateNumberOfMessagesNotVisible])' \
    --output text 2>/dev/null || echo "?/?"
}

# Every queue that exists on this stack, as "label:url" pairs, so nothing below
# needs its own list of which ones are optional.
present() { [ -n "${1:-}" ] && [ "$1" != "None" ]; }
QUEUES=("normalizer (handler raised):$NORM_URL" "rule (undelivered):$RULE_URL")
if present "$RESOLVER_URL"; then
  QUEUES+=("resolver (handler raised):$RESOLVER_URL")
fi
if present "$RESOLVER_RULE_URL"; then
  QUEUES+=("resolver rule (undelivered):$RESOLVER_RULE_URL")
fi

# ONE `depth` CALL PER QUEUE, into a parallel array, reused for both the table and
# the verdict below - see the note under it on why re-querying is a trap.
DEPTHS=()
for pair in "${QUEUES[@]}"; do
  DEPTHS+=("$(depth "${pair#*:}")")
done

# Read each depth ONCE and reuse it. Calling `depth` again for the verdict printed a
# header saying 1 and a verdict saying zero in the same breath: SQS counts are
# eventually consistent ("Approximate" is in the metric name), so two calls a second
# apart legitimately disagree. A tool that contradicts itself is one nobody trusts
# the next time it reports a real dead letter.
NORM_DEPTH="${DEPTHS[0]}"
NORM_COUNT=${NORM_DEPTH%%/*}
RESOLVER_COUNT=0
for index in "${!QUEUES[@]}"; do
  case "${QUEUES[$index]}" in
    resolver*) RESOLVER_COUNT=$(( RESOLVER_COUNT + ${DEPTHS[$index]%%/*} ));;
  esac
done

echo "account $ACCOUNT  region ${AWS_DEFAULT_REGION:-${AWS_REGION:-?}}  stack $STACK"
echo
for index in "${!QUEUES[@]}"; do
  printf "  %-28s %s\n" "${QUEUES[$index]%%:*}" "${DEPTHS[$index]}"
done
echo "  (visible/in-flight, approximate)"
echo

case "$MODE" in
  status)
    if [ "${NORM_COUNT:-0}" = "0" ] && [ "${RESOLVER_COUNT:-0}" = "0" ]; then
      echo "PASS  no dead letters - every fetched payload became events."
    fi
    if [ "${NORM_COUNT:-0}" != "0" ]; then
      echo "$NORM_COUNT dead letter(s) on the normalizer queue."
      echo "  inspect: npm run dlq-peek     replay: npm run dlq-replay"
    fi
    if [ "${RESOLVER_COUNT:-0}" != "0" ]; then
      # A resolver dead letter is a WORSE silence than a normalizer one: the payload
      # was fetched, stored, parsed and conflated, so every upstream metric is green
      # and the corridor is simply missing an event.
      echo "$RESOLVER_COUNT dead letter(s) on the resolver queue(s) - a candidate"
      echo "  was parsed and conflated but never became an event."
      echo "  inspect: npm run dlq-peek"
      echo "  likely causes: an illegal transition (rejected, not coerced),"
      echo "  a persistent write conflict, or an event-store error."
    fi
    ;;

  peek)
    # SQS caps a single receive at 10 regardless of what the user asked for.
    MAX_RECEIVE=$(( PEEK > 10 ? 10 : PEEK ))
    [ "$MAX_RECEIVE" -lt 1 ] && MAX_RECEIVE=1
    for pair in "${QUEUES[@]}"; do
      label="${pair%%:*}"; url="${pair#*:}"
      echo "=== $label"
      # Receive does not delete; the message reappears after the visibility
      # timeout. Reading a DLQ must NEVER be destructive.
      #
      # `--output json` then one-JSON-per-line, rather than `--output text`:
      # a message body is itself JSON containing tabs and newlines, so the text
      # renderer's tab-joining corrupts exactly the bodies worth reading.
      aws sqs receive-message --queue-url "$url" \
        --max-number-of-messages "$MAX_RECEIVE" \
        --visibility-timeout 1 --message-attribute-names All \
        --query 'Messages[].Body' --output json 2>/dev/null \
        | "$PY" -m corridor_event_hub.dlq_format --json-array
    done
    ;;

  replay)
    # BOTH STAGES, and each message decides its own route.
    #
    # A normalizer dead letter replays from the ORIGINAL S3 BYTES: the strong
    # form, where a mapping fix is applied to the exact payload that failed. A
    # resolver dead letter carries an already-parsed candidate and no raw ref, so it
    # replays by handing that candidate back to the resolver - weaker, but the right
    # scope, because the bytes normalized fine and it was resolution that failed.
    # Safe to repeat either way: the content hash makes a re-run of a candidate
    # already in the store a no-op.
    #
    # corridor_event_hub.dlq_format.replay_plan classifies on the message's SHAPE rather than
    # on which queue it came from, so a queue that gets renamed or re-pointed cannot
    # send a message to the wrong handler.
    if [ "${NORM_COUNT:-0}" = "0" ] && [ "${RESOLVER_COUNT:-0}" = "0" ]; then
      echo "nothing to replay."
      exit 0
    fi

    # CDK appends a hash to logical IDs (`NormalizerFnE61C0960`), so match on the
    # PREFIX. An exact-match query returns empty and reads as "the stack is wrong".
    resolve_fn() {
      aws cloudformation describe-stack-resources --stack-name "$STACK" \
        --query "StackResources[?starts_with(LogicalResourceId,'$1')].PhysicalResourceId | [0]" \
        --output text 2>/dev/null
    }
    NORMALIZER_FN=$(resolve_fn NormalizerFn)
    RESOLVER_FN=$(resolve_fn ResolverFn)
    if [ -z "$NORMALIZER_FN" ] || [ "$NORMALIZER_FN" = "None" ]; then
      echo "could not resolve the normalizer function name."
      exit 1
    fi

    echo "replaying dead letters. A message is deleted ONLY after its replay succeeds."
    echo "  normalizer -> $NORMALIZER_FN (from original S3 bytes)"
    echo "  resolver   -> ${RESOLVER_FN:-<not deployed>} (re-resolve the candidate)"
    echo
    replayed=0; failed=0; skipped=0
    # Counted per target, because the two replays make DIFFERENT guarantees and a
    # summary that claims the stronger one for both would overstate what happened.
    replayed_normalizer=0; replayed_resolver=0

    for pair in "${QUEUES[@]}"; do
      label="${pair%%:*}"; queue="${pair#*:}"
      # Bounded rather than `while :`. An unwrappable message is returned to the queue
      # and would otherwise be received forever - an infinite loop in a tool people
      # run when something is already broken.
      for _ in $(seq 1 200); do
        MSG=$(aws sqs receive-message --queue-url "$queue" --max-number-of-messages 1 \
          --visibility-timeout 120 --query 'Messages[0].{B:Body,H:ReceiptHandle}' --output json 2>/dev/null)
        # Explicit tests: `[ -z x ] || [ y ] && break` binds as (||) && and breaks on
        # the wrong condition.
        if [ -z "$MSG" ] || [ "$MSG" = "null" ] || [ "$MSG" = "None" ]; then
          break
        fi

        HANDLE=$("$PY" -c 'import json,sys; print(json.load(sys.stdin)["H"])' <<<"$MSG" 2>/dev/null)
        # Emits "<target>\t<json>" so one call decides both the route and the payload.
        PLAN=$("$PY" -c '
import json, sys
from corridor_event_hub.dlq_format import replay_plan
plan = replay_plan(json.load(sys.stdin)["B"])
if plan is not None:
    target, event = plan
    print(target + "\t" + json.dumps(event))
' <<<"$MSG" 2>/dev/null)

        if [ -z "$PLAN" ]; then
          echo "  SKIP  [$label] no bucket/key and no candidate - cannot replay, left queued"
          skipped=$((skipped+1))
          continue
        fi
        TARGET="${PLAN%%$'\t'*}"
        PAYLOAD="${PLAN#*$'\t'}"

        case "$TARGET" in
          normalizer) FN="$NORMALIZER_FN" ;;
          resolver)   FN="$RESOLVER_FN" ;;
          *)          FN="" ;;
        esac
        if [ -z "$FN" ] || [ "$FN" = "None" ]; then
          echo "  SKIP  [$label] needs the $TARGET function, which is not in this stack"
          skipped=$((skipped+1))
          continue
        fi

        OUT=$(aws lambda invoke --function-name "$FN" \
          --payload "$(printf '%s' "$PAYLOAD" | base64)" \
          --cli-read-timeout 180 /tmp/dlq-replay-out.json 2>&1)
        if printf '%s' "$OUT" | grep -q '"FunctionError"'; then
          echo "  FAIL  [$label] $TARGET replay still failing: $(head -c 200 /tmp/dlq-replay-out.json)"
          echo "        left on the queue - fix the cause and try again."
          failed=$((failed+1))
          # Return it immediately rather than waiting out the visibility timeout.
          aws sqs change-message-visibility --queue-url "$queue" \
            --receipt-handle "$HANDLE" --visibility-timeout 0 >/dev/null 2>&1
          break
        fi
        echo "  ok    [$label] $TARGET: $(head -c 140 /tmp/dlq-replay-out.json)"
        aws sqs delete-message --queue-url "$queue" --receipt-handle "$HANDLE" >/dev/null 2>&1
        replayed=$((replayed+1))
        case "$TARGET" in
          normalizer) replayed_normalizer=$((replayed_normalizer+1)) ;;
          resolver)   replayed_resolver=$((replayed_resolver+1)) ;;
        esac
      done
    done

    echo
    echo "replayed $replayed, still failing $failed, unreplayable $skipped"
    if [ "$replayed_normalizer" -gt 0 ]; then
      echo "  $replayed_normalizer re-normalized from the ORIGINAL S3 bytes, so the"
      echo "  events they produce are identical to what the live run would have."
    fi
    if [ "$replayed_resolver" -gt 0 ]; then
      echo "  $replayed_resolver re-resolved from the parsed candidate. NOT a byte-level replay:"
      echo "  normalization is not re-run, so a mapping fix needs the raw payload instead."
    fi
    ;;

  purge)
    echo "This DISCARDS every dead letter. The raw bytes stay in S3, but the record"
    echo "of what failed to normalize is lost."
    printf 'type PURGE to confirm: '
    read -r ans
    if [ "$ans" != "PURGE" ]; then
      echo "aborted."
      exit 1
    fi
    for pair in "${QUEUES[@]}"; do
      aws sqs purge-queue --queue-url "${pair#*:}" 2>&1 | head -2
    done
    echo "purged (SQS may take up to 60s to reflect it)."
    ;;
esac
