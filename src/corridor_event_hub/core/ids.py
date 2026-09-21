"""Event identifiers.

``event_id`` is a ULID, stable across the whole lifecycle (see types.Event).

WHY ULID AND NOT uuid4. The id is also the DynamoDB partition key and it ends up
in every audit record, every log line, and every API URL. Two properties earn it:

  SORTABLE. The first 48 bits are milliseconds since the epoch, so ids sort by
  creation time as plain strings. "Show me the events created since 14:00" is a
  string comparison rather than a secondary index, and a log file sorted by id is
  sorted chronologically - which is what you want at 2am.

  READABLE. 26 characters of Crockford base32, no hyphens, case-insensitive, and
  the alphabet excludes I, L, O and U so a human reading one off a dashboard
  cannot transcribe it wrong. A uuid4 has neither property.

WHY NOT THE `ulid-py` PACKAGE: forty lines against a dependency in a deployment
bundle this project has worked hard to keep pure Python and small (see
pyproject.toml on shapely). The spec is short and the implementation is testable.

NOT MONOTONIC WITHIN A MILLISECOND. Two ids minted in the same millisecond sort
arbitrarily relative to each other. That is acceptable here because the id orders
EVENTS, and two events created in the same millisecond have no meaningful order;
the ordering that has to be exact is the version and audit sequence within one
event, which is an integer counter in the sort key rather than a timestamp.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

# Crockford base32: no I, L, O or U, so there is nothing to confuse with 1 or 0.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

TIMESTAMP_CHARS = 10  # 48 bits
RANDOM_CHARS = 16  # 80 bits
ULID_LENGTH = TIMESTAMP_CHARS + RANDOM_CHARS


def new_event_id(now: datetime | None = None, entropy: bytes | None = None) -> str:
    """A fresh ULID.

    ``now`` and ``entropy`` are injectable so tests can assert an exact string:
    an id generator nothing can pin is an id generator whose ordering property is
    asserted by nobody.
    """
    moment = now or datetime.now(timezone.utc)
    milliseconds = int(moment.astimezone(timezone.utc).timestamp() * 1000)
    randomness = entropy if entropy is not None else os.urandom(10)
    if len(randomness) != 10:
        raise ValueError("ULID randomness must be exactly 10 bytes (80 bits)")

    return _encode(milliseconds, TIMESTAMP_CHARS) + _encode(
        int.from_bytes(randomness, "big"), RANDOM_CHARS
    )


def _encode(value: int, width: int) -> str:
    """Base32-encode ``value`` right-aligned into ``width`` characters."""
    if value < 0 or value >= 32**width:
        raise ValueError(f"value {value} does not fit in {width} base32 characters")
    out = []
    for _ in range(width):
        out.append(_ALPHABET[value % 32])
        value //= 32
    return "".join(reversed(out))


def timestamp_of(event_id: str) -> datetime:
    """The creation time encoded in a ULID.

    Useful for the bitemporal reconstruction in The id itself carries the
    system time an event first appeared, so a version with a corrupt
    ``created_at`` can still be placed.
    """
    if len(event_id) != ULID_LENGTH:
        raise ValueError(f"not a ULID: {event_id!r} ({len(event_id)} chars)")
    milliseconds = 0
    for char in event_id[:TIMESTAMP_CHARS].upper():
        index = _ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"not a ULID: {event_id!r} has {char!r} outside base32")
        milliseconds = milliseconds * 32 + index
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
