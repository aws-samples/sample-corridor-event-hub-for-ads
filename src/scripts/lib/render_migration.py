"""Render a migration report for a human.

A FILE, not a heredoc, for the reason recorded in tests/test_dlq_format.py: the
dead-letter formatter started life as `python3 -c` inside a single-quoted heredoc
and was broken from its first line, and the only way to find out was to have a
dead letter and try to read it - i.e. during an incident. This renderer has the
same shape of job. It is the thing an operator reads to find out whether a schema
change happened, so it gets to be testable.

Exit code is the contract for scripts/db-migrate.sh:
  0  nothing wrong
  1  drift, an error envelope, or output that is not a report at all

Nonzero for drift even on a --plan run: drift is exactly what a plan exists to
catch, and CI should fail on it rather than print it and pass.
"""

from __future__ import annotations

import json
import sys


def render(raw: str) -> tuple[list[str], int]:
    """Return ``(lines, exit_code)``. Never raises - see the module docstring."""
    try:
        report = json.loads(raw)
    except Exception:
        return ([raw.rstrip()] if raw.strip() else ["no output from the function"], 1)

    if not isinstance(report, dict):
        return ([raw.rstrip()], 1)

    # A raised handler comes back as Lambda's error envelope, not a report. Show
    # the message; the traceback is in CloudWatch and is not what is needed first.
    if "errorMessage" in report:
        lines = ["FAILED", ""]
        lines += [f"  {line}" for line in str(report["errorMessage"]).splitlines()]
        frames = report.get("stackTrace") or []
        if frames:
            lines += ["", f"  ({len(frames)} frames; full traceback in CloudWatch)"]
        return lines, 1

    applied = report.get("applied") or []
    pending = report.get("pending") or []
    unchanged = report.get("unchanged") or []
    drifted = report.get("drifted") or []
    dry_run = bool(report.get("dryRun"))

    lines = [
        f"database  : {report.get('database')}",
        f"migrations: {report.get('migrationDir')}",
        f"tls       : {report.get('tls')}",
        "",
        "DISCOVERED",
    ]
    for item in report.get("discovered") or []:
        lines.append(
            f"  {item['filename']:34s} {item['kind']:10s} {item['statements']:>3} statements"
        )

    lines.append("")
    if dry_run:
        lines.append("WOULD APPLY" if pending else "NOTHING TO APPLY")
        for item in pending:
            lines.append(f"  {item['filename']:34s} ({item['reason']})")
    else:
        lines.append("APPLIED" if applied else "NOTHING TO APPLY")
        for item in applied:
            lines.append(
                f"  {item['filename']:34s} {item['statements']:>3} statements "
                f"in {item['ms']}ms  ({item['reason']})"
            )

    if unchanged:
        lines += ["", "UNCHANGED"] + [f"  {name}" for name in unchanged]

    if drifted:
        lines += ["", "DRIFT - a run-once migration was edited after it was applied"]
        for item in drifted:
            lines.append(f"  {item['filename']}")
            lines.append(f"    {item['advice']}")

    lines.append("")
    if drifted:
        return lines + ["FAILED - see DRIFT above. Nothing was applied."], 1

    if dry_run:
        return lines + [f"PASS  plan only, nothing applied. {len(pending)} file(s) pending."], 0

    lines.append(f"PASS  {len(applied)} applied, {len(unchanged)} unchanged.")
    if applied:
        lines += ["", "Confirm the invariants still hold:  npm run db"]
    return lines, 0


def main() -> int:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    lines, code = render(raw)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
