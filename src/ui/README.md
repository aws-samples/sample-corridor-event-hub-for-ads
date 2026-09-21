# Corridor strip UI

React + Vite app that renders what the pipeline actually produced, served by a local
Python API that runs the real adapters.

```bash
cd src
npm run setup     # once
npm run ui        # API on :8787 + UI on :5173, both stopped by one Ctrl-C
```

Then open <http://localhost:5173>. No AWS account needed. `npm run ui` installs
anything missing itself, so `npm run setup` is only for getting the wait out of the
way up front.

Needs Python >= 3.9 and Node >= 18. If a port is busy the script refuses to start
and names the pid to kill — a fresh front end talking to a stale API would show
data that looks live and is not, which is the one failure this view exists to
prevent.

**Run these from `src/`, not from here** — they drive both halves, and the
Python half is the parent package. Every one has a `make` twin of the same name if
you prefer that.

| Command | Does |
|---|---|
| `npm run ui` | Both processes. The one to use. |
| `npm run serve` | The API alone, on :8787 |
| `npm run serve-fixtures` | The API from captured payloads, no network at all |
| `npm run ui-build` | Production build into `ui/dist` |
| `npm run ui-test` | Layout geometry and trust tests (88) |
| `npm run strip` | Writes `docs/strip/data.json` for scripting or archiving a snapshot |

From inside `ui/` itself, the Vite scripts are `npm run dev`, `npm run build`,
`npm run test`, and `npm run typecheck` — front end only, no Python API.

## Two processes, and why

`strip_server.py` runs the adapters and returns the strip document at
`/api/strip`; Vite proxies `/api` to it, so the app is same-origin and makes a
plain relative `fetch('/api/strip')` — the same call it will make against the real
query API later, with only a base URL changing.

**The API caches, deliberately.** AZ511 allows ten requests per sixty seconds, and
a browser polling every few seconds would get the key throttled — a self-inflicted
outage during a demo. So the adapters re-run at most every 20 seconds and the UI
reports the cached age instead of pretending each poll was a fresh fetch. A forced
refresh cannot bypass that floor; when it is throttled the response says so.

## Freshness is on screen

The header shows the age of the data, colour-coded: green under 2 minutes, amber to
10, red beyond. This exists because the static viewer this replaces could show an
18-hour-old snapshot that looked identical to a live one. **Static data that looks
live** is the worst thing this UI could do.

## Reading the strip

- **One row per source**, plus a MERGED row for canonical events. The gap between
  them is the dedup story (the third of the four problems this pipeline targets).
- **Each row splits into direction bands: EB above, WB below.** Not lanes of road.
  This is the encoding because it reflects a fact about the data: agencies report
  one physical work zone as two records, one per direction. They must stay
  separate — for a truck heading east, the westbound closure is not its problem,
  which is also why the matcher's direction gate refuses to merge them.
- **Within a band, bars stack further only if they would overlap on screen.** That
  extra stacking carries no meaning.
- **The row sub-label counts events**; zoomed, it reads `2 of 9 in view`.
- **`0 events` is often correct.** NWS returns 200 OK but most alerts do not touch
  I-40, and many arrive with `geometry: null` and UGC zone codes only. Empty rows
  state their own reason inline.
- **Bars have a 4px floor**, so a half-mile event looks wider than it is. Zoom for
  true extent.

## No auth, and why

There is no login. The dev API binds to `127.0.0.1` and serves data that is already
public — state DOT feeds and NWS alerts.

A Cognito Hosted UI gate was built here and then removed, because it authenticated
nothing that mattered: the server never verified the token, so the gate blocked the
React view while leaving `:8787` open to anyone who could reach it. **A login prompt
that implies protection it does not provide is worse than no prompt.**

Cloud access, where it happens at all, is **ambient IAM**: `AWS_PROFILE` in the
shell. The API shells out to `aws secretsmanager get-secret-value` for feed keys and
inherits whatever that profile can do, falling back to fixtures when there are no
credentials (ADR 0004). That is a different mechanism from user sign-in and should
not be confused with it.

**When this app is hosted anywhere but localhost**, auth becomes a real requirement —
and the right shape is enforcement at the API, not in the browser: API Gateway with
an authorizer that verifies the JWT against a pool's JWKS. Do that alongside the
deployed query API, where there is something to protect. The `operators` distinction
— who may override a lifecycle transition — belongs in that design too.

## Layout

```
src/
  layout.ts        geometry: scale, lanes, ticks, look-ahead. PURE, no React.
  layout.test.ts   31 tests - the geometry is where the bugs were
  trust.ts         timeline geometry + trust grading + confidence decay. PURE.
  trust.test.ts    57 tests - the projection is where the wrong answers hide
  Strip.tsx        the SVG chart
  Timeline.tsx     event timeline: lifecycle, both clocks, projected confidence
  DetailPanel.tsx  provenance, confidence breakdown, match explanation
  Panels.tsx       source health, review queue, look-ahead, unsourced classes
  App.tsx          shell: freshness, toolbar, layout
  useStripData.ts  fetch + poll + age
  types.ts         the /api/strip wire format
```

Geometry is separated from rendering because **both real defects in the predecessor
were geometry bugs**, and neither was visible by reading the code:

1. Overlapping bars drew exactly on top of each other — the upper one unclickable,
   the lower invisible, so the strip silently under-reported.
2. The first fix compared *measures* with a `span * 0.006` tolerance — 7.45 miles at
   full-corridor zoom — so two Texas work zones 3 miles apart were pushed onto
   separate rows despite being visibly separate. An overlap test has to be in the
   units of the thing it prevents: pixel overlap, so compare painted pixel extents.

Both are now regression tests.

## The event timeline

Selecting a bar draws the event's life on a time axis: the agency's stated window, when
the agency last changed each record, when we fetched, when the TTL fires and which state
it routes to, and confidence projected forward on its class half-life. Beside it are the
legal transitions out of the current state, read from the served table rather
than a copy in TypeScript, and where a disappearance from each feed would send the event.

It is separated into `trust.ts` for the same reason as `layout.ts`: **the wrong answers
here are plausible ones.** Four caveats are load-bearing, and each is a test:

- **No lifecycle history.** `strip_export` holds no state between builds, so every event
  is first observed on every build and its TTL countdown restarts with it. Elapsed time
  in a state is not derivable, and the panel says so rather than presenting `enteredAt`
  as a beginning.
- **Confidence is projected forward only.** Recency is the one component with a law
  describing how it moves, so only its term is decayed — decaying the whole
  value overstates the fall by the weight of everything else. Nothing is drawn to the
  left of the measurement: extrapolating back inflated recency, the total clamped, and
  the chart invented weeks of a perfectly-trusted past.
- **Decay has a floor.** An event resting on corroboration and precise geometry can sit
  above a threshold indefinitely, so "crosses in 3d" is reported only when it is true.
- **A missing timestamp gets no mark.** Feeds that omit `sourceUpdatedAt` are exactly
  the ones whose freshness cannot be vouched for; the scorer falls back to our fetch
  time to keep recency computable, and the freshness chip is labelled `last fetched` and
  graded unknown rather than laundering a poll into a confirmation.

## This is not

- **Not the operator console**. Read-only: no manual transitions, no queue
  actions, and no notion of who is looking. That console needs real authorization,
  because every override's audit record has to carry an operator id.
- **Not a deployment target.** `strip_server.py` is single-process and
  localhost-only. The real query API belongs behind API Gateway per the
  architecture as built.
- **Not a live API client yet.** It reads the local server, not the deployed query
  API; `strip_server.py` stands in for it.
