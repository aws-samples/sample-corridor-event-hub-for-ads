# docs/strip

`data.json` — the exported strip document, written by `npm run strip`.

This directory used to hold a hand-written HTML viewer (`index.html`, `data.js`, and
a self-contained `strip.html`). **That viewer has been replaced by the React app in
[../../ui/](../../ui/)** — one renderer, so a fix cannot land in one and not the
other. Read [ui/README.md](../../ui/README.md) for how to run and read the view.

Run these from `src/`:

```bash
npm run ui              # the app: API on :8787, UI on :5173
npm run strip           # just this file, for scripting or archiving a snapshot
npm run strip-fixtures  # same, from captured payloads only - no network
```

## What this file is for

`data.json` is the canonical artifact, deliberately close in shape to what the query
API must return. Useful for:

- **Scripting** — `jq` over candidates, clusters, and confidence breakdowns.
- **Archiving a snapshot** — a dated copy is a reproducible input, since every
  record carries a `rawRef` to the exact bytes it came from.
- **Diffing two runs** — what changed in the feeds between two points in time.

The React app does not read this file; it gets the same document from
`strip_server.py` over `/api/strip`, which is what makes the freshness indicator
meaningful. Both come from the same `strip_export.build()`.

## Honesty properties carried in the data

- `corridor.verified` and `corridor.warning` — coupled: the warning is non-null
  exactly when the geometry is unverified, and anything rendering this must show it
  when present. Currently `verified: true` with no warning, on real state LRS
  geometry (50–388 m). It read `false` with a ±several-miles warning until the
  placeholder centerline was replaced, and it will read that way again if the
  geometry is ever regenerated from a failing fetch.
- `sources[].mode` — `live`, `fixture`, or `failed`, per source. A document built
  from captured bytes that claimed to be live would be the most misleading thing
  this tool could produce.
- `sources[].issues` — grouped mapping failures with counts. Recorded, never
  dropped or defaulted.
- `unsourcedClasses` — event classes with no adapter, and why. Absence is a finding.
- `clusters[].joins` / `reviewPairs` — every merge and every ambiguous
  non-merge, each with its full score explanation.
