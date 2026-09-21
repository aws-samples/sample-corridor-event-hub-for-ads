/**
 * App shell: freshness, toolbar, and layout.
 *
 * NO AUTH. There was a Cognito Hosted UI gate here; it was removed because it
 * authenticated nothing that mattered. The dev API binds to 127.0.0.1, serves
 * already-public agency data, and never verified the token - so the gate blocked
 * the React view while leaving :8787 wide open to anyone who could reach it. A
 * login prompt that suggests protection it does not provide is worse than no
 * prompt. Cloud access is ambient IAM (AWS_PROFILE); see ui/README.md.
 *
 * FRESHNESS IS PROMINENT BY DESIGN. The static viewer this replaces could show an
 * 18-hour-old snapshot that looked identical to a fresh one. "Static data that
 * looks live" is the failure mode this UI must not have, so the age of the
 * data is in the header, colour-coded, and the poll state is visible.
 */

import { useMemo, useState } from 'react';
import { Strip, type Selection, type TruckState } from './Strip';
import { DetailPanel } from './DetailPanel';
import { LookAhead, ReviewQueue, SourceHealth, Unsourced } from './Panels';
import { useStripData } from './useStripData';
import { ZOOM_CUSTOM, zoomValue, type Viewport } from './layout';
import type { StripCandidate } from './types';

function Freshness({ ageSeconds }: { ageSeconds: number | null }) {
  if (ageSeconds === null) return null;
  const mins = ageSeconds / 60;
  const label =
    ageSeconds < 90
      ? `${Math.round(ageSeconds)}s ago`
      : mins < 90
        ? `${Math.round(mins)}m ago`
        : `${(mins / 60).toFixed(1)}h ago`;
  // Thresholds are the observed feed cadences: the fastest source publishes every
  // 60s and the slowest every 5 min, so past ~10 min the view is behind every feed
  // it draws from and should say so.
  const level = ageSeconds < 120 ? 'fresh' : ageSeconds < 600 ? 'aging' : 'stale';
  return (
    <span className={`freshness ${level}`} title="Age of the data being displayed">
      data {label}
    </span>
  );
}

export function App() {
  const strip = useStripData();

  const [selection, setSelection] = useState<Selection | null>(null);
  // null means "whatever the corridor turns out to be". The previous placeholder of
  // {0, 1241} had to be un-guessed later by an effect that compared against the
  // literal 1241, and any view the user had not touched yet still read as a custom
  // range in the zoom dropdown because 1241 is not the corridor's 1240.7 miles.
  const [zoomedView, setZoomedView] = useState<Viewport | null>(null);
  const [classFilter, setClassFilter] = useState('');
  const [dirFilter, setDirFilter] = useState('');
  const [showMerges, setShowMerges] = useState(true);
  const [showReview, setShowReview] = useState(true);
  const [truck, setTruck] = useState<TruckState>({
    on: false,
    measure: 250,
    direction: 'EB',
    lookAhead: 25,
  });

  const data = strip.data;

  const byId = useMemo(() => {
    const m = new Map<number, StripCandidate>();
    for (const c of data?.candidates ?? []) m.set(c.id, c);
    return m;
  }, [data]);

  const classes = useMemo(
    () => [...new Set((data?.candidates ?? []).map((c) => c.eventClass))].sort(),
    [data],
  );

  if (!data) {
    return (
      <div className="wrap gate">
        <h1>Corridor Event Hub corridor strip</h1>
        {strip.error ? (
          <div className="banner">
            <b>Cannot reach the API.</b> {strip.error}
          </div>
        ) : (
          <p className="sub">Running the adapters&hellip;</p>
        )}
      </div>
    );
  }

  const total = data.corridor.totalMiles;
  const view = zoomedView ?? { min: 0, max: total };
  const zoom = zoomValue(view, data.corridor.states, total);
  const merges = data.clusters.filter((c) => c.members.length > 1).length;
  const liveCount = data.sources.filter((s) => s.mode === 'live').length;

  return (
    <div className="wrap">
      <header>
        <h1>Corridor Event Hub corridor strip</h1>
        <span className="sub">
          {data.corridor.route} &middot; {total.toFixed(0)} mi &middot; {data.candidates.length}{' '}
          candidates &rarr; {data.clusters.length} events &middot; {liveCount}/
          {data.sources.length} feeds live
        </span>
        <Freshness ageSeconds={strip.ageSeconds} />
      </header>

      {data.corridor.warning && (
        <div className="banner">
          <b>Positions are approximate.</b> {data.corridor.warning}
        </div>
      )}

      {merges === 0 && (
        <div className="banner">
          <b>No cross-agency merges in this snapshot.</b> The work-zone feeds cover disjoint
          corridor segments &mdash; Arizona, Texas and Oklahoma never describe the same event,
          so there is nothing to merge. Dedup is exercised by the matcher tests (
          <span className="mono">make test</span>), and will show here once two agencies
          overlap.
        </div>
      )}

      {strip.error && (
        <div className="banner">
          <b>Showing the last good response.</b> {strip.error}
        </div>
      )}

      <div className="toolbar">
        <label className="chk">
          <input
            type="checkbox"
            checked={showMerges}
            onChange={(e) => setShowMerges(e.target.checked)}
          />{' '}
          merge links
        </label>
        <label className="chk">
          <input
            type="checkbox"
            checked={showReview}
            onChange={(e) => setShowReview(e.target.checked)}
          />{' '}
          review links
        </label>

        <span className="chk">
          zoom{' '}
          <span className="mono">
            {view.min.toFixed(0)} &ndash; {view.max.toFixed(0)} mi
          </span>
        </span>
        <select
          value={zoom}
          onChange={(e) => {
            if (e.target.value === ZOOM_CUSTOM) return;
            const s = data.corridor.states.find((v) => v.state === e.target.value);
            setZoomedView(s ? { min: s.beginMeasure, max: s.endMeasure } : { min: 0, max: total });
          }}
          aria-label="Zoom to a state"
        >
          <option value="">full corridor</option>
          {data.corridor.states.map((s) => (
            <option key={s.state} value={s.state}>
              {s.state} ({s.beginMeasure.toFixed(0)}&ndash;{s.endMeasure.toFixed(0)})
            </option>
          ))}
          {/* Only present while a drag-zoom is active, so the control never claims
              a state or the full corridor while showing something else. */}
          {zoom === ZOOM_CUSTOM && <option value={ZOOM_CUSTOM}>dragged range</option>}
        </select>
        <button onClick={() => setZoomedView(null)}>reset</button>

        <span className="spacer" />

        <label className="chk">
          <input
            type="checkbox"
            checked={strip.polling}
            onChange={(e) => strip.setPolling(e.target.checked)}
          />{' '}
          auto-refresh
        </label>
        <button onClick={strip.refresh} disabled={strip.loading}>
          {strip.loading ? 'loading…' : 'refresh'}
        </button>

        <label className="chk" htmlFor="f-class">
          class
        </label>
        <select
          id="f-class"
          value={classFilter}
          onChange={(e) => setClassFilter(e.target.value)}
        >
          <option value="">all</option>
          {classes.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <select value={dirFilter} onChange={(e) => setDirFilter(e.target.value)} aria-label="direction">
          <option value="">all directions</option>
          <option value="EB">EB</option>
          <option value="WB">WB</option>
          <option value="BOTH">BOTH</option>
          <option value="UNKNOWN">UNKNOWN</option>
        </select>
        <button
          className={truck.on ? 'on' : undefined}
          onClick={() => setTruck({ ...truck, on: !truck.on })}
        >
          look-ahead
        </button>
      </div>

      {/* `solo` because the look-ahead panel is the right rail's only occupant: with
          it off, a reserved 380px column just narrows the strip for nothing. */}
      <div className={truck.on ? 'layout' : 'layout solo'}>
        <div>
          <div className="card">
            <Strip
              data={data}
              view={view}
              onViewChange={setZoomedView}
              selection={selection}
              onSelect={setSelection}
              classFilter={classFilter}
              dirFilter={dirFilter}
              showMerges={showMerges}
              showReview={showReview}
              truck={truck}
            />
            <div className="legend">
              {classes.map((c) => (
                <span key={c}>
                  <i className="swatch" style={{ background: `var(--c-${c})` }} />
                  {c}
                </span>
              ))}
              <span>
                <svg width="22" height="8">
                  <line x1="0" y1="4" x2="22" y2="4" className="merge-link" />
                </svg>{' '}
                merged
              </span>
              <span>
                <svg width="22" height="8">
                  <line x1="0" y1="4" x2="22" y2="4" className="review-link" />
                </svg>{' '}
                ambiguous, sent to review
              </span>
              {/* No inline <span> here: a nested inline-flex legend item inside
                  running prose becomes its own flex line and breaks the sentence
                  mid-clause. Plain text only. */}
              <span className="hint">
                Drag to zoom. Bars have a 4px floor, so short events look wider than they are.
                Each source row splits by direction &mdash; EB above, WB below. These are
                directions of travel, not lanes of road: agencies report one work zone as two
                records, one per direction.
              </span>
            </div>
          </div>

          <div style={{ marginTop: 14 }}>
            <DetailPanel
              selection={selection}
              candidates={byId}
              clusters={data.clusters}
              sources={data.sources}
              confidenceModel={data.confidenceModel}
              generatedAt={data.generatedAt}
            />
          </div>

          <div style={{ marginTop: 14 }}>
            <SourceHealth sources={data.sources} />
          </div>

          <div style={{ marginTop: 14 }}>
            <ReviewQueue sources={data.sources} />
          </div>
        </div>

        {truck.on && (
          <div>
            <LookAhead
              truck={truck}
              onChange={setTruck}
              clusters={data.clusters}
              corridor={data.corridor}
            />
          </div>
        )}
      </div>

      {/* Last panel on the page: it is reference material that does not change with
          the data, so it costs the strip no width sitting here. */}
      <div style={{ marginTop: 14 }}>
        <Unsourced classes={data.unsourcedClasses} />
      </div>

      <footer>
        Strip axis is corridor measure (miles from the western terminus), not longitude &mdash;
        the canonical model stores <span className="mono">beginMeasure</span>/
        <span className="mono">endMeasure</span>, so 1-D is the data&rsquo;s native shape
       .
        <br />
        Match model <span className="mono">{data.matchModelVersion}</span>. Served by the local
        dev API, which runs the same adapters the pipeline deploys
        {data.cache && (
          <>
            {' '}
            &mdash; build #{data.cache.buildCount}, adapters re-run at most every{' '}
            {data.cache.minRefreshSeconds}s to respect feed rate limits
            {data.cache.fixturesOnly && ', FIXTURES ONLY (no network)'}
          </>
        )}
        . The real query API is not built yet.
      </footer>
    </div>
  );
}
