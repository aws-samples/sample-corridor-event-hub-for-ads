"""Live feed probe - run this BEFORE any AWS deploy.

    npm run probe          (or: python -m corridor_event_hub.probe)

Fetches the open feeds, runs the real adapters over them, and prints what came
out. No AWS account required, no credentials, no deploy. That is deliberate: anyone
evaluating or adopting this can see the actual data problems in about ten seconds,
before committing to any infrastructure.

It also doubles as the honest-reporting tool. Every mapping issue the adapters find
gets printed, because unmappable values go to a review queue - never dropped,
never defaulted.

The feed list, key resolution, and the public OK token all live in
``adapters/feeds.py``. Two tools running the same adapters over the same feeds must
not be able to disagree about what a source produced - the same reason this file
cross-checks itself against the adapter registry below.
"""

from __future__ import annotations

import math
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

from .adapters.adapter import AdapterContext
from .adapters.feeds import USER_AGENT, FeedTarget, feed_targets
from .adapters.registry import registered_source_ids
from .core.lrs import (
    CORRIDOR_TOTAL_MILES,
    LocalConflator,
    corridor,
    measure_to_state_milepost,
)
from .core.timeutil import now_iso
from .core.types import CandidateEvent

RULE = "=" * 74
FETCH_TIMEOUT_SECONDS = 30
MAX_CANDIDATES_SHOWN = 6


def _fmt_measure(measure: float) -> str:
    if math.isnan(measure):
        return "NaN"
    state_mp = measure_to_state_milepost(measure)
    if state_mp:
        state, milepost = state_mp
        return f"{measure:.1f} ({state} MP {milepost:.1f})"
    return f"{measure:.1f}"


def _print_candidate(candidate: CandidateEvent, index: int) -> None:
    extent = candidate.extent
    if extent.begin_measure == extent.end_measure:
        span = _fmt_measure(extent.begin_measure)
    else:
        span = f"{_fmt_measure(extent.begin_measure)} -> {_fmt_measure(extent.end_measure)}"

    accuracy = extent.positional_accuracy_meters
    accuracy_text = "n/a" if accuracy is None else f"{round(accuracy)}m"
    lanes = (
        "NONE REPORTED"
        if not candidate.lane_impacts
        else " ".join(f"{a.ordinal}:{a.type}={a.status}" for a in candidate.lane_impacts)
    )

    print(f"  [{index + 1}] {candidate.event_class} / {candidate.event_subtype}")
    print(
        f"      extent    {span}  {extent.direction}  "
        f"states={','.join(extent.states) or '-'}"
    )
    print(f"      method    {extent.conflation_method}  accuracy={accuracy_text}")
    print(
        f"      time      {candidate.start_time} -> "
        f"{candidate.end_time or 'OPEN-ENDED'}  ({candidate.time_confidence})"
    )
    print(f"      lanes     {lanes}")
    print(
        f"      agency    severity={candidate.agency_severity or '-'}  "
        f"nativeId={candidate.source.native_id}"
    )


def probe(target: FeedTarget) -> None:
    print(f"\n{RULE}")
    print(f"{target.label} [key: {target.key_source}]")
    print(RULE)

    started = time.monotonic()
    if target.fetcher is not None:
        # A source that is not a single URL GET brings its own fetcher, returning
        # the same (status, body) shape. See FeedTarget.fetcher.
        try:
            status, body = target.fetcher()
        except Exception as exc:  # noqa: BLE001 - same expected outcome as a down feed
            print(f"fetch      ERROR - {type(exc).__name__}: {exc}")
            return
        if status != 200:
            print(f"fetch      FAILED - status {status}, {len(body):,}B")
            return
    else:
        request = urllib.request.Request(
            target.url, headers=target.headers or {}, method="GET"
        )
        try:
            with urllib.request.urlopen(  # nosec B310 # https, enforced by FeedTarget
                request, timeout=FETCH_TIMEOUT_SECONDS
            ) as response:
                body = response.read().decode(
                    response.headers.get_content_charset() or "utf-8", errors="replace"
                )
                status = response.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"fetch      HTTP {exc.code}  {len(body):,}B")
            print(f"           FAILED - {body[:160]}")
            return
        except Exception as exc:  # noqa: BLE001 - a down feed is an expected outcome here
            print(f"fetch      ERROR - {type(exc).__name__}: {exc}")
            return

    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(f"fetch      HTTP {status}  {len(body):,}B  {elapsed_ms}ms")

    result = target.adapter.parse(
        body,
        AdapterContext(
            conflator=LocalConflator(),
            raw_ref="s3://local-probe/not-persisted",
            retrieved_at=now_iso(),
        ),
    )

    print(
        f"adapter    {len(result.candidates)} candidate(s)  "
        f"{result.off_corridor} off-corridor  {len(result.issues)} issue(s)"
    )

    by_class: dict[str, int] = defaultdict(int)
    for candidate in result.candidates:
        by_class[candidate.event_class] += 1
    if by_class:
        print("classes    " + "  ".join(f"{k}={v}" for k, v in by_class.items()))

    if result.candidates:
        print("\ncandidates")
        for index, candidate in enumerate(result.candidates[:MAX_CANDIDATES_SHOWN]):
            _print_candidate(candidate, index)
        if len(result.candidates) > MAX_CANDIDATES_SHOWN:
            print(f"  ... {len(result.candidates) - MAX_CANDIDATES_SHOWN} more")

    # Issues are the point, not noise. Print them.
    if result.issues:
        print("\nmapping issues (would go to the review queue)")
        grouped: dict[str, list] = defaultdict(list)
        for mapping_issue in result.issues:
            grouped[f"{mapping_issue.reason} :: {mapping_issue.field}"].append(mapping_issue)
        for key, group in grouped.items():
            print(f"  {key}  (x{len(group)})")
            detail = group[0].detail
            if detail:
                print(f"      e.g. {detail[:150]}")


def main() -> int:
    print("Corridor Event Hub live feed probe")
    print(
        f"corridor   {corridor.route}  {CORRIDOR_TOTAL_MILES:.0f} mi  "
        + " -> ".join(s.state for s in corridor.states)
    )
    if not corridor.verified:
        print(
            "WARNING    corridor.json verified=false - placeholder centerline and\n"
            "           approximate state mileages. Positional accuracy is +/- miles.\n"
            "           Fine for pipeline evaluation, NOT for publication."
        )

    all_targets = feed_targets()
    targets = [t for t in all_targets if t.live]
    skipped = [t for t in all_targets if not t.live]

    # Derived from the shared feed list rather than hardcoded per source, so a new
    # keyed source reports itself here without anyone remembering to add a note.
    for target in skipped:
        print(f"NOTE       {target.source_id} skipped - no key found. Either:")
        if target.env_var:
            print(f"             export {target.env_var}=<key>   (no AWS needed)")
        if target.secret_id:
            print(
                "             or have AWS credentials with read on secret "
                f"{target.secret_id}"
            )
        if not target.env_var and not target.secret_id and target.fetcher is not None:
            # An IAM-authenticated source has no key to export: the fix is
            # credentials, not a secret. Saying "no key found" without this would
            # send someone hunting for a key that does not exist.
            print(
                "             configure AWS credentials (this source is IAM-authenticated,\n"
                "             there is no API key) - needs geo-maps:GetTile"
            )
    if "set CEH_USER_AGENT" in USER_AGENT:
        print(
            'NOTE       set CEH_USER_AGENT="YourApp (you@example.com)" - NWS policy\n'
            "           requires an identifying User-Agent."
        )

    # Guard against probe/pipeline drift: every adapter the probe exercises must
    # also be registered for the deployed normalizer, or local results would not
    # reflect what AWS does.
    registered = set(registered_source_ids())
    missing = sorted({t.adapter.source_id for t in targets} - registered)
    if missing:
        print(
            "\nWARNING    probing adapters NOT in the pipeline registry: "
            + ", ".join(missing)
        )

    for target in targets:
        probe(target)

    print(f"\n{RULE}")
    print("Still not covered by any feed here: RWIS road-surface SENSING, as")
    print("opposed to weather-derived surface conditions.")
    print()
    print("NOTE ON CONGESTION: the class-4 candidates above come from commercial")
    print("probe data (HERE, via Amazon Location) which is NOT redistributable and")
    print("carries NO confidence field - a speed cannot be told apart from a")
    print("historical average. Good enough to demonstrate the pipeline; it must not")
    print("corroborate an agency-reported closure, and it must not be republished.")
    print("See config/sources.json.")
    print(RULE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
