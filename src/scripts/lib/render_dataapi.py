#!/usr/bin/env python3
"""Render RDS Data API JSON (on stdin) as a plain text table.

Lives in a FILE rather than a heredoc inside db.sh: `python3 - <<'PY'` makes the
heredoc itself become stdin, so a piped payload and the script collide and the
interpreter sees `{"records":...}import sys`. A separate file leaves stdin free
for the data.

The Data API wraps every value in a type tag - {"stringValue": ...},
{"longValue": ...}, {"isNull": true} - so anything reading it must unwrap.
"""
import json
import sys


def cell(value):
    if not isinstance(value, dict):
        return str(value)
    if value.get("isNull"):
        return "NULL"
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in value:
            return str(value[key])
    if "arrayValue" in value:
        return json.dumps(value["arrayValue"])
    return json.dumps(value)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Not JSON: almost always an AWS CLI error string. Surface it verbatim
        # rather than swallowing it - a hidden error reads as "no results".
        text = raw.strip()
        print(f"  {text[:1200]}" if text else "  (no output)")
        return 1 if "error occurred" in text.lower() else 0

    records = payload.get("records") or []
    columns = [
        c.get("label") or c.get("name") or "?"
        for c in (payload.get("columnMetadata") or [])
    ]

    if not records:
        print(f"  (no rows; {payload.get('numberOfRecordsUpdated', 0)} record(s) updated)")
        return 0

    rows = [[cell(v) for v in record] for record in records]
    if not columns:
        columns = [f"col{i + 1}" for i in range(len(rows[0]))]

    widths = [
        max(len(columns[i]), *(len(r[i]) for r in rows)) for i in range(len(columns))
    ]
    print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(columns))))
    for row in rows:
        print("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(row))))
    print(f"\n  {len(rows)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
