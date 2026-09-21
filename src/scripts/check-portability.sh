#!/usr/bin/env bash
#
# Portability check.
#
# The deliverable is a REFERENCE ARCHITECTURE any state DOT can
# adopt, so every state-specific concern must live in exactly one of: a catalog
# entry, a vocabulary/crosswalk artifact, or an adapter module.
# Core services must contain no state names and no corridor identifiers.
#
# "We'll be careful" does not hold across six weeks and multiple people. A build
# failure does. That is why this is CI, and not a code review convention: the
# report names a deprioritized reference architecture as the deliverable most at
# risk, and this is the one part of it a build can enforce mechanically.
#
# Run: npm run check   (or npm run lint:portability)

set -uo pipefail
cd "$(dirname "$0")/.."

FAIL=0

# Directories that MUST stay portable. Adapters and config are exempt by design:
# corridor_event_hub/adapters/ is exactly where state-specific knowledge belongs,
# which is why the adapter registry lives there rather than in handlers/.
#
# Both languages are scanned. The pipeline is Python and the infrastructure is
# TypeScript CDK, and a hardcoded corridor name is just as much of a portability
# break in a stack file as in a handler.
CORE_PATHS=("corridor_event_hub/core" "corridor_event_hub/handlers" "lib" "bin")

# State names and corridor identifiers that must not appear in core.
# Word-boundary matched so 'OKAY' or 'text' do not trip it.
PATTERNS=(
  '\bArizona\b' '\bNew Mexico\b' '\bOklahoma\b'
  '\bADOT\b' '\bNMDOT\b' '\bTxDOT\b' '\bODOT\b'
  '\bI-40\b' '\bI40\b'
  '\baz511\b' '\boktraffic\b' '\bdrivetexas\b'
)

echo "portability check — core must not name states or corridors"
echo "core paths: ${CORE_PATHS[*]}"
echo

# WHY THIS IS NOT JUST A grep: prose explaining WHY something is portable is
# legitimate and often the most valuable text in the file - an adopter needs the
# reasoning, and the reasoning frequently has to name the state whose feed
# forced a decision ("AZ511 alone has twelve spellings of direction").
#
# A line-prefix heuristic handles `#` and `//` comments but NOT Python docstrings,
# which are multi-line strings with no per-line marker. Python's own tokenizer knows
# the difference exactly, so this uses it.
#
# WHAT IS EXEMPT IS ONLY COMMENTS AND DOCSTRINGS. An ordinary string literal is NOT
# exempt: `if source_id == "az511-events"` is precisely the violation this check
# exists to catch, and blanking every STRING token would hide it. A docstring is a
# string that stands alone as a statement; anything else is a value the code uses.
#
# TypeScript has no stdlib tokenizer to hand, so `.ts` files fall back to the
# line-prefix heuristic. Good enough there: CDK code has no docstring equivalent.
scan_python() {
  python3 - "$1" "${PATTERNS[@]}" <<'PY'
import io
import pathlib
import re
import sys
import tokenize

root = pathlib.Path(sys.argv[1])
patterns = [re.compile(p) for p in sys.argv[2:]]
findings = []

# A STRING token is a docstring only if it stands alone as a statement - i.e. the
# previous significant token ended a statement or opened a block.
_STATEMENT_BOUNDARY = frozenset(
    {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
)

for path in sorted(root.rglob('*.py')):
    source = path.read_text(encoding='utf-8')
    lines = source.splitlines()
    stripped = {i: list(line) for i, line in enumerate(lines, start=1)}

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        # Unparseable file: scan it raw rather than skipping it. A file this check
        # cannot read is the last place to assume good behaviour.
        tokens = []

    blank = []
    previous_type = tokenize.ENCODING
    for token in tokens:
        if token.type == tokenize.COMMENT:
            blank.append(token)
        elif token.type == tokenize.STRING and previous_type in _STATEMENT_BOUNDARY:
            blank.append(token)  # a docstring: prose, exempt
        if token.type not in (tokenize.COMMENT, tokenize.NL):
            previous_type = token.type

    for token in blank:
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            if row not in stripped:
                continue
            chars = stripped[row]
            begin = start_col if row == start_row else 0
            finish = end_col if row == end_row else len(chars)
            for col in range(begin, min(finish, len(chars))):
                chars[col] = ' '

    for row, chars in stripped.items():
        code = ''.join(chars)
        if not code.strip():
            continue
        # Report a line ONCE even when several patterns match it - two names on one
        # line is still one thing to fix.
        matched = [p.pattern for p in patterns if p.search(code)]
        if matched:
            findings.append(
                f'{path}:{row}: {lines[row - 1].strip()}   [{", ".join(matched)}]'
            )

for finding in findings:
    print(finding)
sys.exit(1 if findings else 0)
PY
}

for path in "${CORE_PATHS[@]}"; do
  [ -d "$path" ] || continue

  # Python: tokenizer-based, so docstrings are not mistaken for code.
  py_hits=$(scan_python "$path")
  if [ -n "$py_hits" ]; then
    echo "FAIL  state/corridor name in Python CODE under $path"
    echo "$py_hits" | sed 's/^/      /'
    FAIL=1
  fi

  # TypeScript: line-prefix heuristic.
  for pat in "${PATTERNS[@]}"; do
    ts_hits=$(grep -rnE "$pat" "$path" --include='*.ts' 2>/dev/null \
      | grep -vE '^\S+:[0-9]+:\s*(\*|//|/\*)' \
      || true)
    if [ -n "$ts_hits" ]; then
      echo "FAIL  $pat in $path"
      echo "$ts_hits" | sed 's/^/      /'
      FAIL=1
    fi
  done
done

# Bare state abbreviations used as literals. lrs.py is exempt because it reads
# them FROM config - the strings appear there only as the shape of the data it
# loads, never as a hardcoded corridor.
if [ -d corridor_event_hub/core ]; then
  if grep -rnE "['\"](AZ|NM|TX|OK)['\"]" corridor_event_hub/core --include='*.py' 2>/dev/null \
      | grep -vE '^\S+:[0-9]+:\s*#' \
      | grep -v 'lrs\.py' >/dev/null 2>&1; then
    echo "WARN  hardcoded state abbreviation outside lrs.py (which reads them from config)"
  fi
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "PASS  core is portable — state specifics live in adapters and config"
  echo
  echo "note: this check is necessary but not sufficient. It catches names, not"
  echo "      assumptions. A hardcoded milepost range or an adapter-shaped"
  echo "      assumption in core will pass this and still break portability."
else
  echo "FAILED — move the above into an adapter, the source catalog, or a"
  echo "         crosswalk artifact."
fi

exit "$FAIL"
