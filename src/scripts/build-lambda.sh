#!/usr/bin/env bash
#
# Build the Python Lambda deployment bundle.
#
# WHY THIS EXISTS RATHER THAN LETTING CDK DO IT: shapely is a COMPILED package.
# It ships platform-specific wheels carrying a bundled GEOS shared library, so a
# bundle built on a developer's Mac contains macOS arm64 binaries and fails on
# Lambda with:
#
#   Unable to import module 'corridor_event_hub.handlers.collector':
#   /var/task/shapely/lib.cpython-313-darwin.so: invalid ELF header
#
# That failure happens at INVOKE time, after a successful deploy - the most
# expensive place to find it. So this script pins the target platform explicitly
# and pip refuses rather than silently substituting the local wheel.
#
# `PythonFunction` from @aws-cdk/aws-lambda-python-alpha would do this too, but it
# needs Docker, which not every adopter can assume on every machine. Explicit
# wheels need only pip.
#
# Run: npm run bundle     (synth runs this too, via lib/lambda-bundle.ts)
#
# --if-stale exits early when the bundle is newer than every input. That mode
# exists for the synth-time call: a full rebuild re-resolves wheels over the
# network, which is the wrong price for `cdk diff`. `npm run bundle` stays
# UNCONDITIONAL - it is the way to re-resolve wheels, which no mtime can detect.

set -euo pipefail
cd "$(dirname "$0")/.."

IF_STALE=0
for arg in "$@"; do
  case "$arg" in
    --if-stale) IF_STALE=1 ;;
    *) echo "unknown argument: $arg (only --if-stale is accepted)" >&2; exit 2 ;;
  esac
done

BUNDLE_DIR="build/lambda"
# Written only on a PASS, and deliberately outside $BUNDLE_DIR: a marker inside the
# bundle would be uploaded to Lambda and would change the CDK asset hash.
STAMP="build/lambda.stamp"
# Must match lib/ingest-stack.ts: lambda.Runtime.PYTHON_3_13 + ARM_64.
PYTHON_VERSION="3.13"
PLATFORM="manylinux2014_aarch64"

# Prefer the project venv's pip. Cross-platform resolution needs a reasonably
# modern pip: the pip 21 that ships with macOS Command Line Tools evaluates
# `requires-python` against the LOCAL interpreter rather than `--python-version`,
# so it rejects every cp313 wheel with "Packages require a different Python".
# Confusing, and nothing to do with the target platform.
if [ -x .venv/bin/pip ]; then
  PIP=(.venv/bin/pip)
else
  PIP=(python3 -m pip)
fi

# The bundle's inputs, exhaustively: the copies below plus this script, which is
# where the wheel versions are pinned. NOT pyproject.toml - the Lambda's
# dependencies are named here, not there. sql/checks/ is excluded because it is
# diagnostic queries that never enter the bundle.
#
# Paths are literal rather than "$0": cwd is the project root by now, but $0 may be
# a path relative to the caller's cwd, and a find that ERRORS prints nothing - which
# this check would read as "up to date". Hence also the explicit exit status below:
# only a find that succeeded AND printed nothing means fresh.
BUNDLE_INPUTS=(corridor_event_hub config sql/*.sql scripts/build-lambda.sh)

if [ "$IF_STALE" -eq 1 ] && [ -d "$BUNDLE_DIR" ] && [ -f "$STAMP" ]; then
  if NEWER=$(find "${BUNDLE_INPUTS[@]}" -newer "$STAMP" -print -quit) && [ -z "$NEWER" ]; then
    echo "bundle up to date at $BUNDLE_DIR - skipping rebuild (npm run bundle forces one)"
    exit 0
  fi
fi

PIP_VERSION=$("${PIP[@]}" --version | awk '{print $2}' | cut -d. -f1)
if [ "${PIP_VERSION:-0}" -lt 23 ]; then
  cat <<EOF
FAILED - pip ${PIP_VERSION} is too old to resolve wheels for another platform.

It will reject every cp${PYTHON_VERSION/./} wheel with "Packages require a different Python",
which is about the LOCAL interpreter, not the target. Use the project venv:

  npm run venv          # creates .venv with a current pip
  npm run bundle
EOF
  exit 1
fi

echo "building Lambda bundle for python${PYTHON_VERSION} / ${PLATFORM}"

# The stamp goes FIRST: from here until the verification passes there is no valid
# bundle, and a leftover stamp from the previous build would let the next --if-stale
# call adopt a half-built directory.
rm -f "$STAMP"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"

# Runtime dependencies only. boto3 is deliberately absent: the Lambda runtime
# provides it, and shipping our own would add ~15MB to override a library that is
# already there at a matching version.
#
# --only-binary=:all: with an explicit --platform is the whole point. Without it
# pip would happily build or reuse a local wheel and produce a bundle that
# deploys cleanly and cannot import.
#
# pg8000 is the Postgres driver for the migration runner
# (corridor_event_hub/handlers/db_migrate.py). PURE PYTHON, deliberately: its wheel is
# py3-none-any, so unlike shapely there is no platform to get wrong and no second
# compiled extension for the verification below to police. psycopg would be the
# conventional pick and is a fair argument, but DDL is not where a C driver earns
# its keep. It brings scramp and asn1crypto, both pure Python too.
# SHAPELY AND NUMPY ARE DELIBERATELY ABSENT, and their removal is most of this
# script's remaining reason to exist being about pg8000 rather than about GEOS.
#
# Measured before removal: numpy 40.9 MB and shapely 10.0 MB, together 85% of a
# 60 MB bundle. numpy was NEVER IMPORTED by any of our code - it was purely
# shapely's transitive dependency. shapely itself was used in exactly one function,
# for one point-in-polygon boolean on the weather-alert path.
#
# core/lrs.py now answers that with even-odd ray casting in pure Python, verified
# against shapely over 20,000 random points including holes, MultiPolygon and the
# boundary (tests/test_point_in_polygon.py). shapely remains a DEV dependency so
# that comparison keeps running; it is not deployed.
#
# The compiled-wheel platform trap this script was written for therefore no longer
# applies to anything in the bundle. The --platform pins stay because they are what
# makes that STAY true: without them a future dependency would quietly bring a
# manylinux x86 wheel, or a macOS one, and fail at invoke.
"${PIP[@]}" install \
  --quiet \
  --target "$BUNDLE_DIR" \
  --implementation cp \
  --python-version "$PYTHON_VERSION" \
  --platform "$PLATFORM" \
  --only-binary=:all: \
  --upgrade \
  "pg8000>=1.31"

# The application package itself is pure Python, so a copy is enough.
cp -R corridor_event_hub "$BUNDLE_DIR/corridor_event_hub"

# The corridor and the source catalog are CONFIG, and the handlers read
# them at import time. core/config.py looks for config/ beside the package, which
# is what this placement satisfies.
cp -R config "$BUNDLE_DIR/config"

# The schema migrations travel WITH the code that applies them, so a deployed
# function cannot be a version behind the SQL it is meant to run. Same placement
# rule as config/ - core/config.py:sql_dir() looks beside the package.
#
# Top-level *.sql only: sql/checks/ holds diagnostic queries, not migrations, and
# they have no business in a deployment bundle.
mkdir -p "$BUNDLE_DIR/sql"
cp sql/*.sql "$BUNDLE_DIR/sql/"

# The Amazon RDS root CA chain, so the database connection can VERIFY the server
# certificate rather than merely encrypt to it.
#
# WHY IT IS NEEDED AT ALL: the RDS certificate authorities are SELF-SIGNED PRIVATE
# ROOTS. Checked rather than assumed - `Amazon RDS us-west-2 Root CA RSA2048 G1` is its
# own issuer, and the Lambda image's trust store carries `Amazon Root CA 1` but not
# that. So there is no default trust path to Aurora, and a fix that only flipped the
# client's verify flag would have failed every connection instead of failing safe.
#
# WHY FETCHED HERE AND NOT COMMITTED: 165 KB of third-party certificate data on AWS's
# own rotation schedule. A copy in git goes stale silently and shows up in every diff
# it touches. Fetched at build time it is current as of the bundle, and the bundle is
# already the artifact whose freshness this script polices.
#
# WHY NOT AT INVOKE TIME: that puts a network dependency on an external host into the
# connection path, so the thing meant to make the database reachable becomes a new way
# to fail. The global bundle covers every region, which keeps this build step free of
# any assumption about where the stack is deployed.
mkdir -p "$BUNDLE_DIR/certs"
RDS_TRUSTSTORE="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
echo "fetching the RDS root CA chain for TLS verification"
if ! curl -fsSL --max-time 30 -o "$BUNDLE_DIR/certs/rds-global-bundle.pem" "$RDS_TRUSTSTORE"; then
  cat <<EOF
FAILED - could not fetch the RDS CA bundle from:
  $RDS_TRUSTSTORE

This is NOT optional and the build stops here deliberately. Without it the deployed
functions cannot verify the Aurora certificate, and since the RDS roots are self-signed
and absent from the platform trust store, every database connection would fail at
INVOKE time - after a clean deploy, which is the expensive place to find out.

If this host has no egress, fetch the file elsewhere and drop it at:
  $BUNDLE_DIR/certs/rds-global-bundle.pem
EOF
  exit 1
fi

# Strip what only bloats the upload. Compiled caches from the local interpreter
# are worse than useless in the bundle: they are the wrong magic number.
find "$BUNDLE_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLE_DIR" -name '*.pyc' -delete 2>/dev/null || true
find "$BUNDLE_DIR" -name 'tests' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$BUNDLE_DIR"/*.dist-info/RECORD 2>/dev/null || true

# --- verification ----------------------------------------------------------
# Assert the bundle is actually Linux aarch64. A bundle that looks right and is
# built for the wrong platform is the failure mode this script exists to prevent,
# so it is checked rather than assumed.
FAIL=0

if [ ! -d "$BUNDLE_DIR/corridor_event_hub" ]; then
  echo "FAIL  corridor_event_hub package missing from the bundle"
  FAIL=1
fi

if [ ! -f "$BUNDLE_DIR/config/sources.json" ]; then
  echo "FAIL  config/sources.json missing - the collector reads the catalog at import"
  FAIL=1
fi

# THE CORRIDOR MUST NOT BE IN HERE. It moved to reference/ because a corridor in
# config/ was copied into every bundle AND is singular by construction: one route
# per file, which is the cardinality limit the whole spatial refactor removed.
# Deployed functions read corridors from Postgres, keyed by route.
#
# Checked rather than assumed, because the failure is silent in the worst direction:
# a stale bundled corridor would be used in preference to nothing, and would place
# events against geometry the database has since replaced.
if [ -f "$BUNDLE_DIR/config/corridor.json" ] || [ -f "$BUNDLE_DIR/reference/corridor.json" ]; then
  echo "FAIL  a corridor JSON is in the bundle. Production reads corridors from"
  echo "      Postgres (core/postgis.load_corridor); reference/corridor.json is the"
  echo "      OFFLINE source for probe/UI/tests and is not deployed."
  FAIL=1
fi

# The migration runner reads these at INVOKE time, so a bundle without them
# deploys fine and then reports "no migrations found" - which reads like an empty
# schema directory rather than a build problem.
if ! ls "$BUNDLE_DIR"/sql/*.sql >/dev/null 2>&1; then
  echo "FAIL  sql/ missing - the migration runner would find nothing to apply"
  FAIL=1
fi

# The CA chain, asserted PRESENT AND PLAUSIBLE rather than merely present. A captive
# portal or a proxy error page answers with HTTP 200 and a body that is not a
# certificate, and `curl -f` cannot tell the difference - the result would be a bundle
# that deploys and then fails TLS at every invoke, which is exactly the failure class
# this whole script exists to move earlier.
CA_PEM="$BUNDLE_DIR/certs/rds-global-bundle.pem"
if [ ! -f "$CA_PEM" ]; then
  echo "FAIL  certs/rds-global-bundle.pem missing - the DB connection cannot verify the"
  echo "      server certificate, and the RDS roots are not in the platform trust store"
  FAIL=1
elif ! grep -q "BEGIN CERTIFICATE" "$CA_PEM"; then
  echo "FAIL  certs/rds-global-bundle.pem holds no certificate - the fetch returned"
  echo "      something else (a proxy or captive-portal page answers 200 with HTML)"
  FAIL=1
else
  echo "ok    RDS CA chain present ($(grep -c 'BEGIN CERTIFICATE' "$CA_PEM") certificates)"
fi

# Pure-Python driver: assert it is IMPORTABLE from the bundle rather than merely
# present. A --target install that half-resolved leaves the dist-info behind
# without the package, and this handler's failure surfaces at invoke.
if [ ! -f "$BUNDLE_DIR/pg8000/__init__.py" ]; then
  echo "FAIL  pg8000 missing from the bundle - the migration runner cannot connect"
  FAIL=1
elif find "$BUNDLE_DIR/pg8000" -name '*.so' | grep -q .; then
  echo "FAIL  pg8000 has a compiled extension - it is supposed to be pure Python."
  echo "      A platform-specific wheel here reintroduces the ELF-header failure."
  FAIL=1
fi

# NOTHING IN THE BUNDLE MAY CARRY A COMPILED EXTENSION.
#
# This check replaces the one that verified shapely's GEOS was ELF aarch64. The
# invariant is now stronger and cheaper to hold: the deployment bundle is PURE
# PYTHON, so there is no platform to get wrong at all.
#
# Kept as a check rather than dropped, because "pure Python" is a property that
# decays. One `pip install` of something with a C extension reintroduces the whole
# invalid-ELF-header failure class - at invoke time, after a clean deploy. If a
# compiled dependency ever becomes genuinely necessary, verify its platform here
# the way the shapely check used to, rather than deleting this.
COMPILED=$(find "$BUNDLE_DIR" \( -name '*.so' -o -name '*.dylib' -o -name '*.pyd' \) -print 2>/dev/null | head -5)
if [ -n "$COMPILED" ]; then
  echo "FAIL  the bundle is supposed to be PURE PYTHON, but carries compiled objects:"
  echo "$COMPILED" | sed 's/^/        /'
  if command -v file >/dev/null 2>&1; then
    echo "        $(file -b "$(echo "$COMPILED" | head -1)")"
  fi
  echo "      A wheel built for the wrong platform deploys cleanly and fails at"
  echo "      invoke with 'invalid ELF header'. Either drop the dependency or"
  echo "      verify its platform here explicitly."
  FAIL=1
else
  echo "ok    bundle is pure Python - no compiled extensions, no platform to mismatch"
fi

SIZE=$(du -sh "$BUNDLE_DIR" | cut -f1)
echo
if [ "$FAIL" -eq 0 ]; then
  # Stamped only here, so a bundle that failed verification is never treated as
  # fresh by --if-stale on the next synth.
  touch "$STAMP"
  echo "PASS  bundle ready at $BUNDLE_DIR ($SIZE)"
  echo
  echo "note: sql/ is now the largest thing in here - migration DATA, which grows"
  echo "      with every annual vintage and is read only by the migration function."
  echo "      That is the next thing to move if this bundle matters again."
  du -sk "$BUNDLE_DIR"/sql "$BUNDLE_DIR"/config 2>/dev/null \
    | awk '{printf "        %6.2f MB  %s\n", $1/1024, $2}'
else
  echo "FAILED - see above. Do not deploy this bundle."
fi

exit "$FAIL"
