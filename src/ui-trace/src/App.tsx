/**
 * App shell for the record lifecycle tracker.
 *
 * THE HEADER NAMES THE ACCOUNT. Every number in this app comes from one specific
 * deployed stack, and the most expensive mistake available here is reading the wrong
 * one - it looks like a healthy corridor with suspiciously little traffic. So the
 * account, region and table are in the header, not in a settings panel.
 *
 * READ-ONLY IS STATED, not implied. The API underneath has no write path, and the
 * badge says so, because an operator looking at a stuck record will reasonably wonder
 * whether they can force it - and the answer is that overriding is a
 * deliberate, audited, authenticated action that this tool deliberately does not do.
 *
 * NO AUTH, for the same reason ui/ has none: the API binds to 127.0.0.1 and holds
 * ambient IAM credentials. A login prompt in front of it would suggest a protection
 * it does not provide.
 */

import { useMemo, useState } from 'react';
import { Pipeline } from './Pipeline';
import { RawPayload } from './RawPayload';
import { RecordList } from './RecordList';
import { Trace } from './Trace';
import { filterRecords, humanDuration, secondsSince } from './derive';
import { useMeta, useNow, usePipeline, useRecords, useRawPayload, useTrace } from './useTracker';

/** Live states first and selected by default: history is opt-in, not the default view. */
const LIVE_STATES = ['reported', 'validated', 'active', 'clearing'];
const HISTORY_STATES = ['merged', 'cleared', 'archived'];

function Freshness({ ageSeconds }: { ageSeconds: number | null }) {
  if (ageSeconds === null) return null;
  const level = ageSeconds < 60 ? 'fresh' : ageSeconds < 300 ? 'aging' : 'stale';
  return (
    <span className={`freshness ${level}`} title="Age of the listing being displayed">
      read {humanDuration(ageSeconds)} ago
    </span>
  );
}

export function App() {
  const [tab, setTab] = useState<'records' | 'pipeline'>('records');
  const [states, setStates] = useState<string[]>(LIVE_STATES);
  const [query, setQuery] = useState('');
  const [classFilter, setClassFilter] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [polling, setPolling] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);

  const now = useNow();
  const meta = useMeta();
  const records = useRecords({ states, limit: 400, polling: polling && tab === 'records' });
  const trace = useTrace(selected, polling);
  const pipeline = usePipeline(polling && tab === 'pipeline');
  const raw = useRawPayload();

  const rows = useMemo(
    () =>
      filterRecords(records.data?.records ?? [], {
        q: query,
        classes: classFilter ? [classFilter] : [],
        sources: sourceFilter ? [sourceFilter] : [],
        onlyProblems,
      }),
    [records.data, query, classFilter, sourceFilter, onlyProblems],
  );

  const classes = useMemo(
    () => [...new Set((records.data?.records ?? []).map((r) => r.event_class))].sort(),
    [records.data],
  );
  const sources = useMemo(
    () => [...new Set((records.data?.records ?? []).flatMap((r) => r.source_ids))].sort(),
    [records.data],
  );

  const cloud = meta.data?.cloud ?? trace.data?.cloud ?? pipeline.data?.cloud ?? null;
  const listAge = secondsSince(records.data?.generated_at ?? null, now);

  // The one failure worth a full-page gate: no cloud at all means every panel would
  // render an error, and the message carries the fix.
  const blocked = meta.error && !meta.data;

  return (
    <div className="wrap">
      <header>
        <h1>Corridor Event Hub record tracker</h1>
        <span className="sub">
          {cloud ? (
            <>
              <span className="mono">{cloud.account}</span> &middot; {cloud.region} &middot;{' '}
              <span className="mono" title={cloud.event_table}>
                {cloud.event_table.split('-')[1] ?? cloud.event_table}
              </span>
              {cloud.profile && <> &middot; profile {cloud.profile}</>}
            </>
          ) : (
            'resolving the deployed stack…'
          )}
        </span>
        <span className="badge" title="Every AWS call this tool makes is a read. There is no write path.">
          read-only
        </span>
        <Freshness ageSeconds={tab === 'records' ? listAge : null} />
        <span className="spacer" />
        <nav className="tabs">
          <button className={tab === 'records' ? 'on' : undefined} onClick={() => setTab('records')}>
            records
          </button>
          <button className={tab === 'pipeline' ? 'on' : undefined} onClick={() => setTab('pipeline')}>
            pipeline
          </button>
        </nav>
      </header>

      {blocked && (
        <div className="banner">
          <b>Cannot read the deployed stack.</b> {meta.error}
        </div>
      )}

      {!blocked && records.error && tab === 'records' && (
        <div className="banner">
          <b>{records.data ? 'Showing the last good listing.' : 'Cannot list records.'}</b>{' '}
          {records.error}
        </div>
      )}

      {tab === 'records' && (
        <>
          <div className="toolbar">
            <input
              className="search"
              placeholder="event id, agency record id, class, agency, state"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search the fetched records"
            />
            <select value={classFilter} onChange={(e) => setClassFilter(e.target.value)} aria-label="class">
              <option value="">all classes</option>
              {classes.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              aria-label="source"
            >
              <option value="">all sources</option>
              {sources.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            <label className="chk">
              <input
                type="checkbox"
                checked={onlyProblems}
                onChange={(e) => setOnlyProblems(e.target.checked)}
              />{' '}
              only lapsed TTL / unresolved
            </label>

            <span className="spacer" />

            <label className="chk">
              <input
                type="checkbox"
                checked={polling}
                onChange={(e) => setPolling(e.target.checked)}
              />{' '}
              auto-refresh
            </label>
            <button onClick={() => records.reload(true)} disabled={records.loading}>
              {records.loading ? 'reading…' : 'refresh'}
            </button>
          </div>

          <div className="states-bar">
            {[...LIVE_STATES, ...HISTORY_STATES].map((state) => {
              const on = states.includes(state);
              const count = records.data?.fetched_counts_by_state[state];
              return (
                <button
                  key={state}
                  className={`chip s-${state}${on ? ' on' : ''}`}
                  onClick={() =>
                    setStates(on ? states.filter((s) => s !== state) : [...states, state])
                  }
                  title={
                    HISTORY_STATES.includes(state)
                      ? 'History. Reading it costs an extra indexed query per refresh.'
                      : 'Live state'
                  }
                >
                  {state}
                  {on && count !== undefined && <b> {count}</b>}
                </button>
              );
            })}
          </div>

          {records.data?.truncated && (
            <div className="banner">
              <b>Listing truncated.</b> {records.data.truncation_note}
            </div>
          )}

          <div className="layout">
            <div className="list-col">
              <div className="card list-card">
                <h2>
                  {rows.length} of {records.data?.fetched_count ?? 0} fetched
                  <span className="spacer" />
                  <span className="dim">{records.data?.route}</span>
                </h2>
                <RecordList rows={rows} selected={selected} onSelect={setSelected} />
              </div>
            </div>

            <div className="trace-col">
              {trace.error && (
                <div className="banner">
                  <b>Cannot read that trace.</b> {trace.error}
                </div>
              )}
              {!selected && (
                <div className="card">
                  <h2>Pick a record</h2>
                  <p className="sub">
                    Each trace reads the deployed event store: the append-only version chain, the
                    audit record behind every transition, and the S3 payload that caused
                    each one. Nothing here is simulated locally &mdash; the strip UI on{' '}
                    <span className="mono">:5173</span> is the live-adapter view, and it holds no
                    history at all.
                  </p>
                </div>
              )}
              {selected && trace.data && !trace.data.error && (
                <Trace
                  doc={trace.data}
                  meta={meta.data}
                  onOpenRaw={raw.open}
                  onSelect={setSelected}
                />
              )}
              {selected && trace.data?.error && (
                <div className="card">
                  <h2>Not found</h2>
                  <p className="sub">
                    <span className="mono">{selected}</span> is not in the event store. A record that
                    was never resolved has no event id, and a dead-letter message never became one
                    &mdash; check the pipeline tab.
                  </p>
                </div>
              )}
              {selected && !trace.data && trace.loading && <p className="sub">Reading the trace&hellip;</p>}
            </div>
          </div>
        </>
      )}

      {tab === 'pipeline' && (
        <>
          {pipeline.error && (
            <div className="banner">
              <b>Cannot read pipeline health.</b> {pipeline.error}
            </div>
          )}
          {pipeline.data ? (
            <Pipeline doc={pipeline.data} now={now} />
          ) : (
            <p className="sub">Reading&hellip;</p>
          )}
        </>
      )}

      <RawPayload
        payload={raw.payload}
        error={raw.error}
        loading={raw.loading}
        onClose={raw.close}
      />

      <footer>
        Reads the deployed event store directly, so what is shown is the real audit trail rather
        than a local simulation. The corridor snapshot lives in the other UI (
        <span className="mono">npm run ui</span>, port 5173); this one is history.
        {meta.data && (
          <>
            {' '}
            Confidence model <span className="mono">{meta.data.confidence_model.version}</span>,
            match model <span className="mono">{meta.data.match_model.version}</span>, resolver
            policy <span className="mono">{meta.data.resolver_policy_version}</span>.
          </>
        )}
      </footer>
    </div>
  );
}
