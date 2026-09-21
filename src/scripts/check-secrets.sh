#!/usr/bin/env bash
#
# Secret and personal-information scan (DP-4).
#
# WHY THIS EXISTS. A content review of this repository found real personal
# information in three committed test fixtures: a residential address with an
# apartment number paired with a live police dispatch status, 24 phone numbers
# attributed to named contractors and DOT staff, and a named individual's government
# email address. NEITHER cdk-nag NOR ANY INFRASTRUCTURE SCAN CAN FIND THAT, because
# none of them inspect fixture CONTENT - it surfaced only under a content-aware read.
# .ash.yaml configures a scanner that would have had a chance, and nothing ran it.
#
# So the "no personal information" property was a control DEPENDENCY - an assumption
# about contributor diligence - rather than something enforced. DP-4 asks for it to be
# enforced. This is that.
#
# WHY THE FIXTURES ARE THE RISK AND NOT SOMEONE'S CARELESSNESS. Live 511 feeds
# occasionally return citizen-identifying incident data: a law-enforcement dispatch
# record with an address, a contractor's mobile number in a project description. It
# arrives inside an otherwise ordinary payload, and it is NOT distinguishable from
# synthetic test data at a glance (DP-3). Capturing a real payload as a fixture is the
# right practice - the adapters are written against what agency data actually looks
# like - and it is exactly the practice that carries the risk.
#
# WHAT IT LOOKS FOR, and why each pattern rather than a general-purpose scanner: this
# runs in seconds with no network and no install, so it can sit in `npm run check` where a
# heavier scan would get skipped. It is a TRIPWIRE, not a full security scan - .ash.yaml
# configures one of those, and running it stays a deliberate act rather than part of a build.
#
# THE ALLOWLIST IS BY PATH AND PATTERN, WITH A WRITTEN REASON, in the same spirit as
# the cdk-nag suppressions and .ash.yaml: an exception a reader can evaluate. See
# scripts/pii-allowlist.txt. Stale entries are REPORTED rather than left to rot.
#
# Run: npm run check   (or npm run lint:secrets)

set -uo pipefail
cd "$(dirname "$0")/.."

ALLOWLIST="scripts/pii-allowlist.txt"

echo "secret and personal-information scan"
echo "allowlist: $ALLOWLIST"
echo

# TRACKED FILES PLUS UNTRACKED-NOT-IGNORED, which is a correction worth recording.
#
# The first version scanned `git ls-files` only - tracked files. That meant a BRAND NEW
# file was invisible until someone ran `git add`, and the file it was invisible for was
# this one: an early draft of the comment below quoted the real street address as an
# example of the pattern. The scanner could not see its own author making the mistake it
# exists to catch. Anything not yet ignored is a candidate for committing, so it is in
# scope now (`--others --exclude-standard`).
#
# Gitignored trees stay out: scanning node_modules/ or .venv/ would bury the findings
# that matter under hundreds from third-party test data - the same lesson .ash.yaml
# records.
#
# From the REPO ROOT rather than this directory: the fixtures are under src/, but
# the documents at the root are equally published.
python3 - "$ALLOWLIST" <<'PY'
import os
import re
import subprocess
import sys

allowlist_path = sys.argv[1]

# ---------------------------------------------------------------------------
# Patterns. Ordered by how badly a hit would matter.
#
# Each is deliberately NARROW. A scanner that fires on `key` or `token` as words
# produces a hundred findings a day, all of them false, and the response to that is
# to stop reading the output - which is worse than not scanning. Every pattern here
# matches a VALUE with a recognizable shape, not a variable name.
# ---------------------------------------------------------------------------
PATTERNS = [
    (
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "an AWS access key id",
    ),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "a private key block",
    ),
    (
        # The shape of the credential that was once committed here: a long unbroken
        # run of base62 with no word structure. Anchored on assignment or JSON value
        # so prose and hashes in documentation do not trip it.
        "long-opaque-token",
        re.compile(
            r"(?:token|secret|password|passwd|apikey|api_key|access_token|credential)"
            r"[\"']?\s*[:=]\s*[\"']([A-Za-z0-9+/_-]{32,})[\"']",
            re.IGNORECASE,
        ),
        "a credential-shaped literal assigned to a credential-shaped name",
    ),
    (
        # The phone-number half of the fixture finding. 555-0100..555-0199 is the range NANP
        # reserves for fiction, so it is excluded IN THE PATTERN rather than by an
        # allowlist entry - synthetic data should not need an exception.
        "phone-number",
        re.compile(r"(?<!\d)(?:\(\d{3}\)\s*|\d{3}[-.\s])(?!555[-.]01)\d{3}[-.]\d{4}(?!\d)"),
        "a phone number outside the reserved 555-01xx fictional range",
    ),
    (
        # The email half. RFC 2606 / RFC 6761 reserved names are the
        # documented way to write an address that cannot reach anyone.
        "email-address",
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@(?!(?:[A-Za-z0-9-]+\.)*"
            r"(?:example\.(?:com|net|org)|test|invalid|localhost)\b)"
            r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        ),
        "an email address on a domain that is not RFC 2606 reserved",
    ),
    (
        # The address half - and the pattern that took the most tuning,
        # because a corridor dataset is FULL of things shaped like street addresses
        # that are not addresses. The National Bridge Inventory names roads
        # ("40 Frontage Rd", "4092 Local Rd"); route numbers sit next to road names
        # everywhere; a case-insensitive first attempt matched "241 miles of road".
        #
        # A loose pattern here is not the safe choice. Thirty false positives a run
        # teaches everyone to ignore the output, and then it catches nothing at all.
        #
        # So this matches the two things that made the real address IDENTIFY SOMEONE
        # rather than merely look like an address, either of which is enough:
        #
        #   1. a UNIT DESIGNATOR - apt/unit/suite plus a number. Roads do not have
        #      apartment numbers; dwellings do. This is the strongest single signal
        #      that a string refers to where a person lives.
        #   2. a house number followed by a DIRECTIONAL PREFIX and a street type -
        #      "<number> N <NAME> RD", the form the real one took. The directional is
        #      what distinguishes a postal address from a road name in agency prose.
        #      The real value is NOT quoted here: it is the personal information this
        #      check exists to keep out, and an example in a comment is still committed.
        #
        # WHAT THIS DELIBERATELY MISSES: a bare "123 Main Street". Accepted, and
        # recorded rather than papered over - this is a tripwire against the specific
        # way real addresses have entered these fixtures, not a substitute for the
        # content-aware review DP-3 asks for before a payload is committed.
        "street-address",
        re.compile(
            r"(?:\b(?:apt|apartment|unit|ste|suite)\.?\s*#?\s*\d+\b)"
            r"|"
            r"(?<!\d)\d{2,5}\s+[NSEW]\.?\s+[A-Z][A-Za-z]{2,}(?:\s+[A-Z][A-Za-z]+)?\s+"
            r"(?:RD|ROAD|ST|STREET|AVE|AVENUE|BLVD|BOULEVARD|DR|DRIVE|LN|LANE)"
            r"\b(?![A-Za-z])",
            re.IGNORECASE,
        ),
        "a unit designator or a directional street address - see the note in the script",
    ),
]

# Binary and generated content. A .png has no prose to review, and a lockfile's
# integrity hashes are high-entropy by design.
SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2",
    ".drawio", ".svg",
)
SKIP_NAMES = ("package-lock.json", "LICENSE")

# THE ALLOWLIST ITSELF IS NOT SCANNED, and the circularity is the reason. Its whole job
# is to quote the values it permits, so every entry would need an entry to excuse the
# entry - and the first thing that produces is somebody allowlisting the allowlist by
# wildcard, which is worse. Standard treatment for a suppression file.
#
# THE RESIDUAL RISK, stated rather than glossed: a real credential pasted into a
# `reason` field would not be caught here. Nothing about an exception file makes that
# safe - it is committed like any other line - so the rule is to name what is allowed,
# never to paste a value that is not already elsewhere in the repository.
SKIP_BASENAMES_EXTRA = (os.path.basename(allowlist_path),)


def load_allowlist(path):
    """Entries are `path :: regex :: reason`, one per line. `#` comments, blanks ok.

    The REASON IS MANDATORY - a two-field entry is rejected. An exception whose
    justification was not worth typing is not one a reviewer can evaluate, and this
    file is read by whoever inherits the repository.
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
                print(f"FAIL  {path}:{lineno}: expected `path :: regex :: reason`, got {line!r}")
                sys.exit(1)
            file_glob, pattern, reason = parts
            entries.append(
                {
                    "glob": file_glob,
                    "regex": re.compile(pattern),
                    "reason": reason,
                    "line": lineno,
                    "used": False,
                }
            )
    return entries


def allowed(entries, path, text):
    import fnmatch

    for entry in entries:
        if fnmatch.fnmatch(path, entry["glob"]) and entry["regex"].search(text):
            entry["used"] = True
            return True
    return False


root = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    capture_output=True, text=True, check=True,
).stdout.strip()

tracked = subprocess.run(
    ["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()

allowlist = load_allowlist(allowlist_path)
findings = []
scanned = 0

for rel in tracked:
    base = os.path.basename(rel)
    if rel.endswith(SKIP_SUFFIXES) or base in SKIP_NAMES or base in SKIP_BASENAMES_EXTRA:
        continue
    full = os.path.join(root, rel)
    try:
        with open(full, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        continue  # binary or unreadable: nothing to review by eye either
    scanned += 1

    for number, line in enumerate(lines, 1):
        for name, pattern, description in PATTERNS:
            for match in pattern.finditer(line):
                hit = match.group(0)
                if allowed(allowlist, rel, hit):
                    continue
                findings.append((rel, number, name, description, hit))

# ---------------------------------------------------------------------------
# Stale allowlist entries are reported, not silent. An entry that matches nothing -
# because a line moved, or because the data was scrubbed - is an exception standing
# open for a finding that no longer exists, and it will excuse the next one.
# ---------------------------------------------------------------------------
stale = [e for e in allowlist if not e["used"]]

print(f"scanned {scanned} committed-or-committable text files with {len(PATTERNS)} patterns")
print(f"allowlist: {len(allowlist)} entries, {len(stale)} matched nothing")
print()

if stale:
    print("  STALE ALLOWLIST ENTRIES (re-pin or delete):")
    for entry in stale:
        print(f"    {allowlist_path}:{entry['line']}  {entry['glob']}  -  {entry['reason'][:70]}")
    print()

if findings:
    print(f"  {len(findings)} FINDING(S):")
    shown = {}
    for rel, number, name, description, hit in findings:
        shown.setdefault(name, []).append((rel, number, description, hit))
    for name, items in sorted(shown.items()):
        print(f"\n  [{name}] {items[0][2]}")
        for rel, number, _description, hit in items[:12]:
            # The hit is TRUNCATED and the file/line printed instead. Echoing a
            # credential in full puts it in CI logs, which is a second disclosure.
            preview = hit if len(hit) <= 24 else hit[:21] + "..."
            print(f"    {rel}:{number}  {preview}")
        if len(items) > 12:
            print(f"    ... and {len(items) - 12} more")
    print()

if findings or stale:
    print("FAILED")
    print()
    print("A real finding: replace the value with synthetic data (the reserved")
    print("555-01xx phone range, RFC 2606 example.com domains, fictional names and")
    print("addresses) - or, for a credential, move it to Secrets Manager and point")
    print("`secretId` at it (ADR 0004).")
    print()
    print(f"A false positive: add a `path :: regex :: reason` line to {allowlist_path}.")
    print("The reason is required, and a reader has to be able to evaluate it.")
    sys.exit(1)

print("PASS  no unexplained credentials or personal information in committable files")
PY
