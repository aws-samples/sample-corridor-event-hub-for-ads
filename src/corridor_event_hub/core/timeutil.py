"""Timestamp handling.

Agency feeds are inconsistent about time in ways that fail silently rather than
loudly, which is why this is one shared module rather than a parse call in each
adapter:

  - NWS and WZDx write ISO 8601, sometimes with 'Z', sometimes with '+00:00'.
    ``datetime.fromisoformat`` on Python 3.9 rejects 'Z' outright, so a naive
    implementation loses every NWS timestamp on a stock macOS python3.
  - AZ511 writes UNIX EPOCH SECONDS. A parse that accepts anything numeric turns
    junk into 1970, and a 1970 start time silently corrupts every TTL timer
    downstream.
  - Some feeds omit the offset entirely.

Time is load-bearing for lifecycle, so an unparseable timestamp must
become ``None`` and a mapping issue, never a plausible wrong answer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Sanity window for epoch timestamps: 2000-01-01 to 2100-01-01.
EPOCH_MIN = 946_684_800
EPOCH_MAX = 4_102_444_800


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning None rather than raising.

    A naive timestamp is treated as UTC: agency feeds that omit an offset are
    publishing UTC in every case observed, and the alternative (guessing a local
    zone per state) would be worse than an explicit, documented assumption.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def iso_utc(moment: datetime) -> str:
    """Render UTC with a trailing 'Z' and milliseconds, matching the feeds."""
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def now_iso() -> str:
    return iso_utc(datetime.now(timezone.utc))


def now_utc() -> datetime:
    """``now_iso``'s datetime sibling.

    Exists because the resolver and the query API do arithmetic with now - TTL
    windows, confidence decay, "is this inside its active window" - and parsing a
    string this module just formatted would be a round trip with two chances to
    lose the timezone. One call site for "what time is it" also means a test can
    monkeypatch one thing.
    """
    return datetime.now(timezone.utc)


def duration_minutes(start_iso: str | None, end_iso: str | None) -> int | None:
    """Minutes between two ISO timestamps, or None if either is unusable."""
    start = parse_iso(start_iso)
    end = parse_iso(end_iso)
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 60)


def epoch_to_iso(value: Any) -> str | None:
    """Epoch seconds -> ISO 8601. Returns None rather than 1970 for junk input.

    Rejects anything outside a plausible window, which also catches the
    millisecond-epoch mistake: a feed switching units would otherwise land in the
    year 58000 without complaint.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if value <= 0 or value < EPOCH_MIN or value > EPOCH_MAX:
        return None
    return iso_utc(datetime.fromtimestamp(value, tz=timezone.utc))
