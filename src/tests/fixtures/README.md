# Test fixtures — what is real and what is not

These files are the replay path: the same bytes, the same adapter, the same output,
forever. **Three are genuine agency captures and three are generated**, and the
difference changes what a passing test proves. Read this before citing a fixture as
evidence of anything about a live feed.

| Fixture | Origin | What a passing test proves |
|---|---|---|
| [ok-odot-wzdx.json](ok-odot-wzdx.json) | **Captured** from oktraffic.org, 2026-08-07/08 | The adapter handles what Oklahoma actually sent |
| [tx-dot-wzdx.json](tx-dot-wzdx.json) | **Captured** from api.drivetexas.org, 2026-08-07/08 | Same, for Texas |
| [nws-alerts.json](nws-alerts.json) | **Captured** from api.weather.gov, 2026-08-07 | Same, for NWS |
| [az511-events.json](az511-events.json) | **Generated** by [`scripts/make-synthetic-fixtures.py`](../../scripts/make-synthetic-fixtures.py) | The adapter handles the shapes we RECORDED AZ511 having |
| [nm-dot-weathershare.json](nm-dot-weathershare.json) | **Generated**, same script | Same, for New Mexico |
| [aws-location-traffic.json](aws-location-traffic.json) | **Generated**, same script | Same, for Amazon Location traffic tiles |

## Why three are generated: the redistribution evidence

Not preference — licensing. This repository is published under **MIT No Attribution**,
the most permissive licence in the set, which grants every reader the right to reuse
anything in it. We can only grant that for bytes whose source granted it to us.

The `redistributable` flag in [config/sources.json](../../config/sources.json) is *our
own catalog's claim*, not an authority. Below is what each claim actually rests on.
**Verified 2026-09-15.** These are the references, not legal advice, and terms change —
re-check before a release.

### Redistributable: kept as captured bytes

| Source | Evidence | Where to re-verify |
|---|---|---|
| `ok-odot-wzdx` | The payload **declares its own licence**: `road_event_feed_info.license` = `https://creativecommons.org/publicdomain/zero/1.0/` — CC0-1.0, a public-domain dedication by the publisher, carried in the very bytes we redistribute | `road_event_feed_info.license` in [ok-odot-wzdx.json](ok-odot-wzdx.json) |
| `tx-dot-wzdx` | Same, in `feed_info.license` | `feed_info.license` in [tx-dot-wzdx.json](tx-dot-wzdx.json) |
| `nws-alerts` | NOAA/NWS: *"The information on National Weather Service (NWS) Web pages are in the public domain, unless specifically noted otherwise, and may be used without charge for any lawful purpose"* — plus 17 U.S.C. § 105, under which a work of the US Government is not subject to copyright | <https://www.weather.gov/disclaimer> |

The two WZDx grants are the strongest form of evidence available: **in-band and
publisher-asserted.** They are also not incidental — the WZDx specification *requires*
`license` to be exactly that CC0 URL (an enum of one; see
[reference/wzdx/4.2/FeedInfo.json](../../reference/wzdx/4.2/FeedInfo.json)), so a
conformant WZDx feed is CC0 by construction and both of these comply.

NWS carries **three conditions** rather than none: do not claim it as your own, do not
imply NOAA/NWS endorsement, and do not modify it and present the result as official
government material. This fixture is an unmodified capture, labelled as a test fixture
and attributed to NWS, which satisfies all three — but a *modified* NWS fixture would
not, so that one must never be edited or generated.

### Not granted: generated instead

| Source | What the catalog records | Why that is not permission |
|---|---|---|
| `aws-location-traffic` | `redistributable: false`; `license: "HERE content licensed via AWS. Attribution MANDATORY."`; `attribution: "(c) 2026 HERE"` | An explicit refusal. Third-party (HERE) content sublicensed through AWS, with attribution obligations MIT-0 cannot carry |
| `az511-events` | `license: "unknown - confirm redistribution terms with ADOT"` | **Unknown is not granted.** A vendor-operated platform, and the terms were never confirmed |
| `nm-dot-weathershare` | `license: "unknown - aggregator terms unstated AND NMDOT terms unconfirmed"` | Two unknowns: the aggregator's terms and the originating DOT's |

The two `unknown` entries are the honest ones to sit with. The safe reading of an
unconfirmed term is that the right was not granted — the same default the pipeline
applies to a source that stops reporting. If someone confirms terms with ADOT or the
WeatherShare operator, the fixture can go back to being a capture and the generator
entry can be deleted; until then, generating is the only option that does not assume
permission nobody gave.

## What the generated fixtures cost

**They prove nothing about the real feeds.** They prove the adapter handles the shapes
we recorded the real feeds having — a weaker claim, and the weakness is the point of
writing it down. The observations themselves, with dates and counts, are in
[docs/DATA-SOURCES.md](../../docs/DATA-SOURCES.md). A reader can verify the adapter;
they cannot verify the observation.

The generator states, per record, which observed characteristic that record exists to
reproduce — the pair-encoded route fields, the epoch-seconds timestamps, the 5 prose
mentions of I-40 of which only 2 are on it, the free-flow-dominant segment mix. Those
comments are the specification. If a test stops depending on one of them, delete the
record rather than leaving it as scenery.

Two properties are load-bearing and enforced rather than hoped for:

- **Coordinates come from the real corridor**, never typed in. Every on-corridor record
  sits on a corridor centerline vertex from [reference/corridor.json](../../reference/corridor.json),
  so a synthetic record cannot drift off the road and start testing the conflation
  buffer instead of what it was written for. That is not hypothetical: earlier fixtures
  used town centroids outside the 1,600 m buffer, and the tests passed vacuously over
  empty candidate lists.
- **The tile fixture is real MVT.** `aws-location-traffic.json` carries base64 protobuf
  encoded by the exact inverse of the decoder in
  [core/mvt.py](../../corridor_event_hub/core/mvt.py), so [test_mvt.py](../test_mvt.py)
  decodes it with the production decoder rather than around it.

## Regenerating

```bash
python scripts/make-synthetic-fixtures.py           # all three
python scripts/make-synthetic-fixtures.py az511     # one
python scripts/make-synthetic-fixtures.py --check   # fail if a file is stale
```

Edit the generator, not the JSON. A hand-edit to a generated fixture is silently
reverted by the next run, and `--check` is what tells you it happened.

The three captured fixtures have no generator and must not get one. They were also
content-reviewed for personal information — one AZ511 record had arrived with a
residential address pasted into `RoadwayName` — and `scripts/check-secrets.sh` keeps
checking.
