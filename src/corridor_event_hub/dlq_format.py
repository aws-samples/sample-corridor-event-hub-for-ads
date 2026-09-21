"""Render dead-letter messages for a human.

WHY THIS IS A MODULE AND NOT INLINE IN dlq.sh: it started as `python3 -c` inside a
single-quoted shell heredoc, where every inner quote has to be escaped - and escaped
quotes inside an f-string are a SyntaxError before Python 3.12. The script "worked"
right up to the first time anyone actually had a dead letter to look at, which is
precisely the moment you least want your inspection tool to be broken.

Here it is importable, testable, and runs on the same interpreter as the rest of the
pipeline.

TWO ENVELOPE SHAPES, because the two failure paths deliver different things:

  Lambda destination (onFailure)   wraps the original event in `requestPayload` and
                                   adds `responsePayload` with the error, plus
                                   `requestContext.condition`. The error is why the
                                   queue is worth reading.

  EventBridge target DLQ           delivers the BARE event, with the reason in SQS
                                   message attributes rather than the body.
"""

from __future__ import annotations

import json
from typing import Any

MAX_ERROR_CHARS = 160
MAX_RAW_CHARS = 200


def parse_message(body: str) -> dict[str, Any]:
    """Normalize either envelope shape into one flat dict for display."""
    try:
        msg = json.loads(body)
    except ValueError:
        return {"unparseable": body[:MAX_RAW_CHARS]}

    if not isinstance(msg, dict):
        return {"unparseable": str(msg)[:MAX_RAW_CHARS]}

    # A Lambda destination envelope carries the original event under
    # `requestPayload`; an EventBridge DLQ message IS the event.
    request = msg.get("requestPayload")
    inner = request if isinstance(request, dict) else msg
    detail = inner.get("detail") if isinstance(inner.get("detail"), dict) else {}

    response = msg.get("responsePayload")
    response = response if isinstance(response, dict) else {}
    context = msg.get("requestContext")
    context = context if isinstance(context, dict) else {}

    return {
        "source_id": detail.get("sourceId"),
        "bucket": detail.get("bucket"),
        "key": detail.get("key"),
        "retrieved_at": detail.get("retrievedAt"),
        "checksum": detail.get("checksum"),
        "condition": context.get("condition"),
        "error_type": response.get("errorType"),
        "error_message": response.get("errorMessage"),
        # WHICH STAGE failed, from what the message carries - see replay_plan.
        "stage": (
            "normalizer"
            if detail.get("bucket") and detail.get("key")
            else "resolver"
            if isinstance(detail.get("candidate"), dict)
            else None
        ),
        "event_class": (detail.get("candidate") or {}).get("event_class")
        if isinstance(detail.get("candidate"), dict)
        else None,
        "replayable": bool(
            (detail.get("bucket") and detail.get("key"))
            or isinstance(detail.get("candidate"), dict)
        ),
    }


def format_message(parsed: dict[str, Any], index: int) -> list[str]:
    """One dead letter as indented lines."""
    if "unparseable" in parsed:
        return [f"  [{index}] <unparseable body> {parsed['unparseable']}"]

    source_id = parsed.get("source_id") or "?"
    condition = parsed.get("condition") or "n/a"
    lines = [f"  [{index}] source={source_id}  condition={condition}"]

    error_type = parsed.get("error_type")
    if error_type:
        message = str(parsed.get("error_message") or "")[:MAX_ERROR_CHARS]
        lines.append(f"       error : {error_type}: {message}")

    if parsed.get("key"):
        lines.append(f"       bytes : s3://{parsed.get('bucket')}/{parsed.get('key')}")
    if parsed.get("retrieved_at"):
        lines.append(f"       when  : {parsed.get('retrieved_at')}")
    if parsed.get("event_class"):
        lines.append(f"       class : {parsed.get('event_class')}")

    # The operational point: say whether `npm run dlq-replay` can act on this, and by
    # WHICH route - the two stages replay differently and the distinction decides
    # whether a mapping fix or a resolution fix is what this message is waiting on.
    stage = parsed.get("stage")
    if stage == "normalizer":
        lines.append("       replay: yes - re-normalize the original S3 bytes")
    elif stage == "resolver":
        lines.append("       replay: yes - re-resolve the parsed candidate (idempotent)")
    else:
        lines.append("       replay: NO - no bucket/key and no candidate; inspect by hand")
    return lines


def replay_event(body: str) -> dict[str, Any] | None:
    """Unwrap a dead letter back into the event the normalizer handler expects.

    Returns None when the message carries no usable detail, so a caller can leave it
    queued rather than invoking the handler with garbage.
    """
    plan = replay_plan(body)
    return plan[1] if plan and plan[0] == "normalizer" else None


#: Which handler replays which dead letter, keyed by what the detail carries.
#:
#: A logical-ID PREFIX, because CDK appends a hash (`NormalizerFnE61C0960`) - the
#: same reason dlq.sh matches on prefix rather than an exact name.
REPLAY_TARGETS = {
    "normalizer": "NormalizerFn",
    "resolver": "ResolverFn",
}


def replay_plan(body: str) -> tuple[str, dict[str, Any]] | None:
    """Which function can replay this dead letter, and with what event.

    Returns ``(target, event)`` or None if nothing can act on it.

    THE TWO STAGES REPLAY DIFFERENTLY, and the difference is which evidence the
    message still carries:

      normalizer  the detail names an S3 bucket and key, so replay means re-running
                  the adapter over THE ORIGINAL BYTES. That is the strong
                  form: a mapping fix is applied to the exact payload that failed,
                  and the result is identical to what the live run would have
                  produced.

      resolver    the detail carries an already-parsed candidate and no raw ref, so
                  replay means handing that candidate back to the resolver. Weaker -
                  it re-runs resolution rather than normalization - but it is the
                  right scope: the bytes normalized fine, and it was resolution that
                  failed. Safe to repeat because the content hash makes a re-run of
                  an already-stored candidate a no-op.

    Classified on the detail's SHAPE rather than on which queue it came from. A
    queue can be renamed or re-pointed; what the message contains is what determines
    what can be done with it.
    """
    try:
        msg = json.loads(body)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None

    request = msg.get("requestPayload")
    inner = request if isinstance(request, dict) else msg
    detail = inner.get("detail") if isinstance(inner.get("detail"), dict) else None
    if not detail:
        return None

    if detail.get("bucket") and detail.get("key"):
        return "normalizer", {"detail": detail}
    if isinstance(detail.get("candidate"), dict):
        return "resolver", {"detail": detail}
    return None


def _main() -> int:
    """Render SQS message bodies from stdin.

    ``--json-array`` reads the shape ``aws sqs receive-message --output json`` emits
    for ``--query 'Messages[].Body'``: a JSON array of body strings, or ``null`` for
    an empty queue. Without the flag, one body per line.
    """
    import sys

    argv = sys.argv[1:]
    raw = sys.stdin.read()

    bodies: list[str] = []
    if "--json-array" in argv:
        try:
            loaded = json.loads(raw) if raw.strip() else None
        except ValueError:
            loaded = None
        bodies = [b for b in (loaded or []) if isinstance(b, str)]
    else:
        bodies = [stripped for stripped in (line.strip() for line in raw.splitlines()) if stripped]

    for index, body in enumerate(bodies, start=1):
        for out in format_message(parse_message(body), index):
            print(out)
    if not bodies:
        print("  (empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
