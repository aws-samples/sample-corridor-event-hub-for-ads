# Corridor Event Hub for ADS.
#
# The pipeline is Python; the infrastructure is TypeScript CDK. This Makefile is
# the single entry point across both, so nobody has to remember which half of the
# project a given task belongs to.
#
#   make install    everything, both languages
#   make probe      run the adapters against live feeds - START HERE, no AWS needed
#   make test       the Python test suite
#   make check      everything CI checks, in the order it checks it
#   make deploy     bundle + cdk deploy
#
# Python is pinned to a venv rather than the system interpreter: the Lambda bundle
# needs a modern pip for cross-platform wheels (see scripts/build-lambda.sh), and
# macOS ships pip 21.
#
# The venv is built with the newest interpreter on the box, preferring 3.13 -
# the Lambda runtime (lib/ingest-stack.ts) - so local behaviour matches deployed.
# `python3` is the last resort, not the default: on macOS it is 3.9, which still
# works (pyproject floors there on purpose) but makes boto3 print a
# PythonDeprecationWarning into every process. Override with `make PYTHON=...`.
#
# The installer directories are searched explicitly for the same reason
# scripts/run-ui.sh appends them for `aws`: a make- or IDE-launched shell often has
# a narrower PATH than the terminal, so a Homebrew python3.13 is invisible here
# even though it works interactively - and the venv silently lands on 3.9.
PYTHON ?= $(shell \
  { for d in "" /opt/homebrew/bin/ /usr/local/bin/; do \
      for v in 3.13 3.12 3.11; do command -v "$${d}python$$v" 2>/dev/null; done; \
    done; command -v python3; } | head -1)

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help venv install install-py install-cdk install-ui install-trace-ui probe serve serve-fixtures \
        ui ui-build ui-test trace trace-ui trace-ui-build trace-ui-test \
        test test-v fixtures lint-fixtures lint fmt bundle synth diff deploy destroy check status logs clean \
        dlq dlq-peek dlq-replay dlq-purge

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	@$(PY) -c 'import sys; v=sys.version_info; print(f"venv python {v.major}.{v.minor}.{v.micro}")'

venv: $(VENV)/bin/activate ## Create the Python venv

install-py: venv ## Install Python deps (runtime + dev)
	$(PIP) install --quiet -e '.[dev]'

install-cdk: ## Install CDK deps
	npm install

install-ui: ## Install UI deps (React app)
	cd ui && npm install

install-trace-ui: ## Install tracker UI deps (the second React app)
	cd ui-trace && npm install

install: install-py install-cdk install-ui install-trace-ui ## Install everything

probe: install-py ## Run adapters against LIVE feeds - no AWS account needed
	$(PY) -m corridor_event_hub.probe

# The strip UI is two processes: a Python API that runs the real adapters, and the
# Vite dev server that proxies /api to it. `make ui` runs both and cleans up the
# API when you Ctrl-C the front end - otherwise a stray server keeps the port and
# the next `make ui` silently talks to a stale one.
serve: install-py ## Run the strip API alone on :8787
	$(PY) -m corridor_event_hub.strip_server

serve-fixtures: install-py ## Run the strip API from captured payloads only, no network
	$(PY) -m corridor_event_hub.strip_server --fixtures

# Two processes, one Ctrl-C.
#
# Delegated to a script rather than inlined, because getting this right needs more
# than a one-line trap and the failure is nasty: an orphaned API keeps :8787, and
# the next `make ui` silently talks to a stale server.
#
# What went wrong inline, recorded so nobody reinvents it:
#   - killing `$!` reaps the `sh -c` wrapper and ORPHANS the python child
#   - writing a pid file then running `cd ui` breaks the trap's relative path
#   - a trap alone leaks when the shell is signalled while blocked on a child
ui: install-py install-ui ## Run the corridor strip app (API + React dev server)
	@bash scripts/run-ui.sh

ui-build: install-ui ## Production build of the UI into ui/dist
	cd ui && npm run build

ui-test: install-ui ## Run the UI test suite (layout geometry)
	cd ui && npm run test

# The SECOND local UI, on different ports so it runs alongside the first.
#
# Two apps because they answer two questions from two different sources: the strip
# runs the adapters and shows the corridor NOW, the tracker reads the deployed event
# store and shows what happened to one record. Merging them would mean one app that
# needs an AWS account for half its screens.
trace: install-py ## Run the tracker API alone on :8788 (reads the DEPLOYED stack)
	$(PY) -m corridor_event_hub.trace_server

trace-ui: install-py install-trace-ui ## Run the record lifecycle tracker (API :8788 + React :5174)
	@bash scripts/run-trace-ui.sh

trace-ui-build: install-trace-ui ## Production build of the tracker into ui-trace/dist
	cd ui-trace && npm run build

trace-ui-test: install-trace-ui ## Run the tracker UI test suite (step grouping, bands, filters)
	cd ui-trace && npm run test

test: install-py ## Run the Python test suite
	$(PY) -m pytest

test-v: install-py ## Run the test suite with test names
	$(PY) -m pytest -v

fixtures: install-py ## Regenerate the three GENERATED fixtures (see tests/fixtures/README.md)
	$(PY) scripts/make-synthetic-fixtures.py

lint-fixtures: install-py ## Fail if a generated fixture was hand-edited
	$(PY) scripts/make-synthetic-fixtures.py --check

lint: install-py ## Lint Python
	$(VENV)/bin/ruff check src tests

fmt: install-py ## Format Python
	$(VENV)/bin/ruff format src tests
	$(VENV)/bin/ruff check --fix src tests

bundle: install-py ## Build the linux/aarch64 Lambda bundle
	bash scripts/build-lambda.sh

# synth and diff do NOT depend on bundle, because lib/lambda-bundle.ts builds it
# during synth when an input changed. Forcing it here as well would put a full wheel
# re-resolve - network, ~60MB - in front of every `make diff`.
#
# deploy keeps the prerequisite on purpose: it is the command that ships bytes, so it
# re-resolves the wheels and prints the platform verification first. The synth-time
# build then finds the bundle fresh and does nothing.
synth: install-py ## cdk synth
	npx cdk synth

diff: install-py ## cdk diff
	npx cdk diff

deploy: bundle ## Build the bundle and deploy every stack
	npx cdk deploy --all

destroy: ## Tear down. NOTE: the raw S3 bucket is RETAIN by design.
	npx cdk destroy --all

check: ## Everything CI checks
	bash scripts/check.sh

status: ## Health of a deployed stack, one screen
	bash scripts/status.sh

logs: ## Live tail both functions
	bash scripts/logs.sh

# --- dead letters (R-6.7) ---------------------------------------------------
# A payload that failed to normalize is REPLAYABLE: the raw bytes are still in S3
# under Object Lock, so `dlq-replay` re-runs the fixed adapter over the original
# bytes rather than re-fetching from the agency (R-6.6).

.PHONY: dlq dlq-peek dlq-replay dlq-purge
dlq:        ## Depth of both dead-letter queues (should be zero)
	bash scripts/dlq.sh

dlq-peek:   ## Show what is in the dead-letter queues, non-destructively
	bash scripts/dlq.sh --peek 10

dlq-replay: ## Re-normalize dead letters from the original S3 bytes (R-6.6)
	bash scripts/dlq.sh --replay

dlq-purge:  ## Discard all dead letters (asks for confirmation)
	bash scripts/dlq.sh --purge

clean: ## Remove build artifacts (keeps the venv and node_modules)
	rm -rf build cdk.out .pytest_cache .ruff_cache ui/dist ui-trace/dist
	find src tests -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- spatial database (RDS Data API) ---------------------------------------
# ARNs are DISCOVERED per account, never hardcoded: the stack has already been
# deployed to two accounts, and pasted ARNs fail in a way that looks like an
# unreachable database.

.PHONY: db db-info db-bootstrap db-landmarks db-migrate db-migrate-plan db-history \
        db-check db-inventory db-geometry db-nbi corridor corridor-offline \
        nbi nbi-offline nbi-plan
db:            ## smoke-test the spatial DB (postgis, row counts, LRS invariant)
	bash scripts/db.sh

# --- what is actually loaded ------------------------------------------------
# Four read-only checks, cheapest first. `db-check` runs all of them, which is the
# answer to "I loaded something, did it work?".
#
# Each is a SINGLE statement in sql/checks/, passed to db.sh so the results are
# RENDERED. `db.sh --file` would report ok/FAIL per statement and show none of the
# rows, which is the wrong shape for a check whose output IS the answer.

db-check:      ## every read-only check: inventory, geometry, NBI, landmarks
	@bash scripts/db-check.sh

db-inventory:  ## row counts per table, with what each SHOULD be
	bash scripts/db.sh "$$(grep -v '^--' sql/checks/inventory.sql)"

db-geometry:   ## did the real centerline land, and is its M calibrated?
	bash scripts/db.sh "$$(grep -v '^--' sql/checks/geometry.sql)"

db-nbi:        ## NBI load health: known clearances vs conflated vs usable (C-3)
	bash scripts/db.sh "$$(grep -v '^--' sql/checks/nbi.sql)"

db-info:       ## show which account/cluster/secret the DB scripts resolve to
	bash scripts/db.sh --info

# --- schema changes ---------------------------------------------------------
# db-migrate is the one to use. It runs INSIDE the VPC, records every application
# in schema_migration, and wraps each file in a transaction - so a file either
# applied or it did not.
#
# db-bootstrap is the older Data API path, kept because it needs no deployed
# Lambda and is therefore the way to apply a schema to a cluster whose migration
# function has not been deployed yet. Two caveats: it records NOTHING (a later
# db-migrate will apply 001 again, which is safe only because that file is
# idempotent), and it keeps going after a failed statement.

db-migrate-plan: ## what db-migrate WOULD apply. Changes nothing. Run this first.
	bash scripts/db-migrate.sh --plan

db-migrate:    ## apply pending sql/ migrations via the in-VPC Lambda (recorded)
	bash scripts/db-migrate.sh

db-history:    ## what has been applied, and when
	bash scripts/db.sh "SELECT filename, left(checksum,12) AS checksum, statements, \
	  repeatable, applied_at, applied_ms, applied_by FROM schema_migration ORDER BY filename"

db-bootstrap:  ## apply sql/001-init.sql over the Data API - UNRECORDED, see above
	bash scripts/db.sh --file sql/001-init.sql

db-landmarks:  ## conflation accuracy vs known mileposts - catches SYSTEMATIC bias
	bash scripts/db.sh "$$(grep -v '^--' sql/checks/landmarks.sql)"

# --- corridor geometry ------------------------------------------------------
# Regenerates sql/002-corridor-real.sql from the four states' own LRS layers.
# Needs network but NO AWS account. Applying it is a separate step - db-migrate.
#
# Prints the landmark check before writing, so a bad fetch is visible without
# touching the database. See docs/CORRIDOR-GEOMETRY.md.

corridor:      ## rebuild the I-40 centerline from state ARNOLD/LRS services
	$(PY) scripts/fetch-arnold.py

# --- NBI bridge clearances (class 7) ---------------------------------------
# Regenerates sql/003-nbi-structures.sql from the FHWA National Bridge Inventory.
# Public domain, no credential. Needs network but NO AWS account; applying it is a
# separate step - db-migrate.
#
# ANNUAL, not a feed. Rerun when FHWA publishes a new vintage (change NBI_YEAR),
# not on a schedule - which is why nbi-bridges stays out of verified_live in
# config/sources.json.

nbi:           ## rebuild the NBI clearance migration from FHWA's annual files
	$(PY) scripts/fetch-nbi.py

nbi-offline:   ## same, from build/nbi-cache - no network
	$(PY) scripts/fetch-nbi.py --offline

nbi-plan:      ## what the NBI load WOULD contain. Writes no file, needs no AWS.
	$(PY) scripts/fetch-nbi.py --offline --dry-run

corridor-offline: ## same, from build/arnold-cache - no network
	$(PY) scripts/fetch-arnold.py --offline
