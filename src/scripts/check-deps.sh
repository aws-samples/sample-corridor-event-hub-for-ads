#!/usr/bin/env bash
#
# Dependency vulnerability audit, both ecosystems.
#
# WHY THIS EXISTS. `npm run check` ran linting, tests, type-checking and cdk-nag, and
# no vulnerability scan at all. The security review also raised seven unpinned-
# dependency findings across the package manifests and build scripts - standard
# supply-chain hygiene rather than an active vulnerability - and hand-pinning them now
# would trade one problem for a worse one: a pinned tree nobody updates goes stale
# silently, and the version that matters is the one resolved at build time. An audit
# is the control that makes a floating range SAFE to keep, because a new advisory
# against a resolved version fails a build instead of waiting for someone to look.
#
# TWO ECOSYSTEMS, and both have to be here. The pipeline is Python and the
# infrastructure is TypeScript CDK; auditing one and not the other covers half a
# supply chain and reads as though it covered all of it.
#
# THIS IS THE ONE CHECK IN `npm run check` THAT NEEDS THE NETWORK, and that is a real
# difference from every other step. Both audits query an advisory database, so this
# SKIPS rather than fails when it cannot reach one - a plane, a locked-down network or
# a machine behind a proxy must not block the other nineteen checks. A skip
# is printed loudly, because a silent skip is how a gate stops being a gate.
#
# WHAT FAILS AND WHAT ONLY REPORTS. High and critical fail. Moderate and low are
# printed and do not, because at this project's dependency count a moderate advisory
# in a transitive build-time package is a thing to schedule rather than a thing to
# stop for - and a check that fails constantly gets bypassed, which costs more than
# it saves.
#
# Run: npm run check   (or npm run lint:deps)

set -uo pipefail
cd "$(dirname "$0")/.."

ADVISORIES="scripts/dep-advisories.txt"

REPORTS=$(mktemp -d)
trap 'rm -rf "$REPORTS"' EXIT

FAIL=0
SKIPPED=()

echo "dependency vulnerability audit"
echo "accepted advisories: $ADVISORIES"
echo

# ---------------------------------------------------------------------------
# Reachability, checked ONCE and cheaply. Without this the failure mode is two
# separate multi-second timeouts reported as though they were findings.
# ---------------------------------------------------------------------------
if ! curl -fsS --max-time 8 -o /dev/null https://registry.npmjs.org/-/ping 2>/dev/null; then
  cat <<'EOF'
SKIP  no reachable advisory database (the npm registry did not answer in 8s).

      This is the only check in `npm run check` that needs the network. Everything
      else - the tests, the template assertions, cdk-nag - runs offline, and does.

      Run `npm run lint:deps` when you are back online, and treat a green
      `npm run check` from an offline machine as unaudited rather than clean.
EOF
  exit 0
fi

# ---------------------------------------------------------------------------
# npm: three manifests. The CDK app, the corridor strip UI, the record tracker UI.
#
# Audited per package rather than once, because each has its OWN lockfile and
# `npm audit` reads the lockfile of the directory it runs in. One invocation at the
# root would report on the CDK tree and silently ignore both UIs - which is the shape
# of gap this whole finding is about.
# ---------------------------------------------------------------------------
audit_npm() {
  local dir="$1" label="$2"

  if [ ! -f "$dir/package-lock.json" ]; then
    echo "  SKIP  $label - no package-lock.json (run 'npm install' in $dir)"
    SKIPPED+=("$label")
    return 0
  fi

  # Via a FILE rather than interpolated into the Python source. `npm audit --json`
  # emits advisory titles containing quotes and backslashes, and pasting that into a
  # heredoc'd script is a syntax error waiting for the right CVE description.
  local report
  report=$(mktemp)
  # `|| true`: a nonzero exit here means "found something", which is data rather than
  # an error, and the counts below are what decide pass or fail.
  (cd "$dir" && npm audit --json 2>/dev/null) > "$report" || true

  if [ ! -s "$report" ]; then
    echo "  SKIP  $label - npm audit produced no output"
    SKIPPED+=("$label")
    rm -f "$report"
    return 0
  fi

  python3 - "$label" "$dir" "$ADVISORIES" "$report" <<'PY'
import json
import os
import sys

label, directory, advisories_path, report_path = sys.argv[1:5]

try:
    with open(report_path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    print(f"  SKIP  {label} - could not parse npm audit output")
    sys.exit(2)


def accepted_advisories(path):
    """`package :: advisory id or url :: reason`, one per line.

    WHY A SUPPRESSION FILE EXISTS AT ALL. The first advisory this check found was a
    DoS in `brace-expansion`, BUNDLED inside aws-cdk-lib and therefore not updatable
    on its own - `npm audit fix` says so. That one was answerable with a patch bump of
    aws-cdk-lib, and it was fixed rather than suppressed. The next one will not always
    be, and a check with no way to say "reviewed, does not apply" has exactly one
    outcome: somebody removes it from `npm run check`.

    So: an advisory can be accepted, in writing, with a reason a reader can evaluate -
    the same discipline as the cdk-nag suppressions and .ash.yaml. And an entry that
    matches nothing FAILS, so an acceptance cannot outlive the advisory it excused.
    """
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("::")]
            if len(parts) != 3 or not all(parts):
                print(f"  FAIL  {path}:{lineno}: expected `package :: advisory :: reason`")
                sys.exit(1)
            entries.append(
                {"package": parts[0], "advisory": parts[1], "reason": parts[2],
                 "line": lineno, "used": False}
            )
    return entries


accepted = accepted_advisories(advisories_path)
gating, excused = [], []

for name, vuln in sorted((data.get("vulnerabilities") or {}).items()):
    if vuln.get("severity") not in ("critical", "high"):
        continue
    via = [v for v in (vuln.get("via") or []) if isinstance(v, dict)]
    titles = sorted({v.get("title", "") for v in via})
    urls = {v.get("url", "") for v in via} | {str(v.get("source", "")) for v in via}
    detail = "; ".join(t for t in titles if t)[:110]

    match = next(
        (
            e for e in accepted
            if e["package"] == name and any(e["advisory"] in u for u in urls if u)
        ),
        None,
    )
    if match:
        match["used"] = True
        excused.append((name, vuln["severity"], match["reason"]))
    else:
        gating.append((name, vuln["severity"], detail))

counts = (data.get("metadata") or {}).get("vulnerabilities") or {}
line = ", ".join(
    f"{k} {counts.get(k, 0)}" for k in ("critical", "high", "moderate", "low")
)

for name, severity, reason in excused:
    print(f"  note  {label} - {severity} in {name} ACCEPTED: {reason[:80]}")

if gating:
    print(f"  FAIL  {label} - {line}")
    # Name the packages. A count with no package name is not actionable, and the first
    # thing anyone does with one is re-run the command by hand to find out.
    for name, severity, detail in gating:
        print(f"          {severity:8} {name}  {detail}")
    print(f"          fix: (cd {directory} && npm audit fix)   then review the diff")
    print(f"          or accept it in writing: {advisories_path}")
    sys.exit(1)

trailer = "  (moderate/low reported, not gating)" if counts.get("moderate") or counts.get("low") else ""
print(f"  ok    {label} - {line}{trailer}")
sys.exit(0)
PY

  local status=$?
  [ "$status" -eq 1 ] && FAIL=1
  [ "$status" -eq 2 ] && SKIPPED+=("$label")
  # Kept, not deleted: the staleness pass at the end of this script re-reads every
  # report at once. Per-package staleness cannot be decided per package - an entry for
  # the CDK app matches nothing in the ui audit, and calling that stale would be wrong.
  mv "$report" "$REPORTS/$(echo "$dir" | tr '/.' '__').json"
  return 0
}

echo "  npm (3 manifests - the CDK app and both UIs):"
audit_npm "." ". (CDK app)"
audit_npm "ui" "ui (corridor strip)"
audit_npm "ui-trace" "ui-trace (record tracker)"

# ---------------------------------------------------------------------------
# Python. pg8000 is the only runtime dependency, but the DEV tree is what runs the
# tests that gate every deploy, so both are audited.
#
# pip-audit is a dev dependency (pyproject.toml). Absent means someone has not re-run
# `npm run setup` since it was added, which is a skip with an instruction rather than
# a failure - the same treatment check.sh gives absent UI node_modules.
# ---------------------------------------------------------------------------
echo
echo "  python:"
PIP_AUDIT=".venv/bin/pip-audit"
if [ ! -x "$PIP_AUDIT" ]; then
  echo "  SKIP  pip-audit is not installed - run 'npm run install-py' to add it"
  SKIPPED+=("python")
else
  # NOT --strict, and the reason is worth recording because --strict is the flag you
  # would reach for first. It makes any unauditable distribution an error, and
  # `corridor-event-hub` is installed editable from this checkout, so --strict fails
  # PERMANENTLY on this repository's own package - a check that can never pass is a
  # check that gets commented out.
  #
  # What --strict was wanted for is still enforced, one line down: the skip list is
  # asserted to contain nothing but `corridor-event-hub`. "Audited everything except our own
  # code" is a claim worth making; "audited everything I happened to resolve" is not,
  # and only the assertion tells them apart.
  AUDIT_OUT=$("$PIP_AUDIT" --skip-editable --progress-spinner off 2>&1)
  AUDIT_STATUS=$?

  UNEXPECTED_SKIPS=$(printf '%s\n' "$AUDIT_OUT" \
    | awk '/^Name +Skip Reason/{flag=1; next} /^-+ +-+$/{next} flag && NF && $1 != "corridor-event-hub"')

  if [ "$AUDIT_STATUS" -ne 0 ]; then
    echo "  FAIL  pip-audit reported findings:"
    echo "$AUDIT_OUT" | sed 's/^/          /'
    FAIL=1
  elif [ -n "$UNEXPECTED_SKIPS" ]; then
    echo "  FAIL  pip-audit skipped a package that is not this repository's own:"
    echo "$UNEXPECTED_SKIPS" | sed 's/^/          /'
    echo "          A skipped distribution is an unaudited one. Resolve it or record why."
    FAIL=1
  else
    echo "  ok    $(printf '%s\n' "$AUDIT_OUT" | head -1) (corridor-event-hub itself skipped: editable)"
  fi
fi

# ---------------------------------------------------------------------------
# STALE ACCEPTED ADVISORIES. Decided once, over every report at once, because an entry
# for the CDK app legitimately matches nothing in the ui audit.
#
# An acceptance that matches no advisory is worse than no acceptance: it is a written
# exception standing open, and the next reader takes it as evidence the package was
# reviewed. Same rule .ash.yaml states for its own suppressions.
# ---------------------------------------------------------------------------
if [ -f "$ADVISORIES" ]; then
  echo
  python3 - "$ADVISORIES" "$REPORTS" <<'PY' || FAIL=1
import glob
import json
import os
import sys

advisories_path, reports_dir = sys.argv[1], sys.argv[2]

live = set()
for report in glob.glob(os.path.join(reports_dir, "*.json")):
    try:
        with open(report, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        continue
    for name, vuln in (data.get("vulnerabilities") or {}).items():
        for via in vuln.get("via") or []:
            if isinstance(via, dict):
                live.add((name, via.get("url", "")))
                live.add((name, str(via.get("source", ""))))

stale = []
with open(advisories_path, encoding="utf-8") as handle:
    for lineno, line in enumerate(handle, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("::")]
        if len(parts) != 3:
            continue  # already reported by the per-package pass
        package, advisory, reason = parts
        if not any(package == n and advisory in (u or "") for n, u in live):
            stale.append((lineno, package, advisory, reason))

if stale:
    print("  FAIL  accepted advisories that no longer match anything:")
    for lineno, package, advisory, reason in stale:
        print(f"          {advisories_path}:{lineno}  {package}  {advisory}")
        print(f"                  was: {reason[:88]}")
    print("          The advisory is gone or the package moved. DELETE the entry -")
    print("          an acceptance left standing will excuse the next finding silently.")
    sys.exit(1)

print("  ok    no stale accepted advisories")
PY
fi

echo
if [ ${#SKIPPED[@]} -gt 0 ]; then
  echo "  NOT AUDITED: ${SKIPPED[*]}"
  echo "  A partial audit is not a clean one. Resolve the skips before quoting this."
  echo
fi

if [ "$FAIL" -eq 0 ]; then
  echo "PASS  no high or critical advisories against the resolved dependency tree"
  exit 0
fi

cat <<'EOF'
FAILED - a high or critical advisory against something this build resolves.

The unpinned ranges in the manifests are deliberate (see the note at the top of this
script): an audit is what makes them safe, so a finding here is the control working
rather than a surprise. Update the dependency, or - if the advisory does not apply to
how this code uses the package - record that in writing next to the pin, the way
.ash.yaml and the cdk-nag suppressions do.
EOF
exit 1
