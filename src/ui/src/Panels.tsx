/**
 * Source health, the review queue, look-ahead, and unsourced classes.
 *
 * These are the panels that keep the view honest. The strip shows what WAS placed
 * on the corridor; these show what was fetched, what could not be mapped, and what
 * has no feed at all. Without them a thin snapshot looks like a healthy one.
 */

import { Fragment } from 'react';
import { classColor } from './Strip';
import { distanceAhead, isAhead } from './layout';
import { sourceName } from './sourceNames';
import type { TruckState } from './Strip';
import type { StripCluster, StripData, StripSource } from './types';

function fmtBytes(n: number | null): string {
  if (n === null) return '–';
  if (n < 1024) return `${n}B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)}KB`;
  return `${(n / 1048576).toFixed(1)}MB`;
}

export function SourceHealth({ sources }: { sources: StripSource[] }) {
  return (
    <div className="card">
      <h2>Source health</h2>
      <table>
        <thead>
          <tr>
            <th>source</th>
            <th>mode</th>
            <th>http</th>
            <th>bytes</th>
            <th className="num">cand</th>
            <th className="num">off</th>
            <th className="num">issues</th>
          </tr>
        </thead>
        <tbody>
          {sources.map((s) => {
            const issueTotal = s.issues.reduce((n, i) => n + i.count, 0);
            return (
              <Fragment key={s.sourceId}>
                <tr>
                  <td title={`${s.sourceId} — ${s.label}`}>{sourceName(s.sourceId)}</td>
                  <td>
                    <span className={`pill ${s.mode}`}>{s.mode}</span>
                  </td>
                  <td className="mono">
                    {s.httpStatus ?? '–'}
                    {s.latencyMs !== null && <span className="row-sub"> {s.latencyMs}ms</span>}
                  </td>
                  <td className="mono">{fmtBytes(s.payloadBytes)}</td>
                  <td className="num">{s.candidateCount}</td>
                  <td className="num">{s.offCorridor}</td>
                  <td className="num">{issueTotal}</td>
                </tr>
                {s.note && (
                  <tr>
                    <td colSpan={7} className="row-sub warn-text">
                      {s.note}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      <div className="note">
        <b>off</b> = records the adapter saw but could not place on this corridor. High counts
        are usually correct: most weather alerts in four states do not touch I-40.
        <br />
        <b>cand = 0 is not a failure.</b> NWS is the usual case: it fetches 200 OK, but most
        active alerts do not touch the route, and many arrive with{' '}
        <span className="mono">geometry: null</span> and UGC zone codes only &mdash; those
        cannot be placed without a zone shapefile join, so they are recorded as issues rather
        than guessed at.
      </div>
    </div>
  );
}

export function ReviewQueue({ sources }: { sources: StripSource[] }) {
  const rows = sources.flatMap((s) =>
    [...s.issues].sort((a, b) => b.count - a.count).map((i) => ({ src: s.sourceId, ...i })),
  );

  return (
    <div className="card">
      <h2>Review queue &mdash; unmappable values</h2>
      {rows.length === 0 ? (
        <p className="empty">No mapping issues in this snapshot.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>reason</th>
              <th>field</th>
              <th>source</th>
              <th className="num">n</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <Fragment key={i}>
                <tr>
                  <td>{r.reason}</td>
                  <td className="mono">{r.field}</td>
                  <td className="row-sub" title={r.src}>
                    {sourceName(r.src)}
                  </td>
                  <td className="num">{r.count}</td>
                </tr>
                {r.example && (
                  <tr>
                    <td colSpan={4} className="row-sub dimmer">
                      e.g. {r.example.slice(0, 150)}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
      <div className="note">
        Recorded, never dropped or defaulted. A steady rate is the system working; a spike
        means a feed changed shape.
      </div>
    </div>
  );
}

export function Unsourced({ classes }: { classes: StripData['unsourcedClasses'] }) {
  return (
    <div className="card">
      <h2>Not sourced yet</h2>
      <table>
        <thead>
          <tr>
            <th>class</th>
            <th>why</th>
          </tr>
        </thead>
        <tbody>
          {classes.map((u) => (
            <tr key={u.eventClass}>
              <td>{u.eventClass}</td>
              <td className="row-sub">{u.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="note">Absence is a finding, not an omission. See DATA-SOURCES.md.</div>
    </div>
  );
}

interface LookAheadProps {
  truck: TruckState;
  onChange: (t: TruckState) => void;
  clusters: StripCluster[];
  corridor: StripData['corridor'];
}

/**
 * The only view that asks the question the consumer actually asks: given
 * position, heading, and a look-ahead distance, what is ahead?
 */
export function LookAhead({ truck, onChange, clusters, corridor }: LookAheadProps) {
  const position = { measure: truck.measure, direction: truck.direction };
  const hits = clusters
    .filter((cl) => isAhead(cl, position, truck.lookAhead))
    .map((cl) => ({ cl, dist: distanceAhead(cl, position) }))
    .sort((a, b) => a.dist - b.dist);

  const state = corridor.states.find(
    (s) => truck.measure >= s.beginMeasure && truck.measure <= s.endMeasure,
  );
  const mp = state ? `${state.state} MP ${(truck.measure - state.beginMeasure).toFixed(1)}` : null;

  return (
    <div className="card">
      <h2>Look-ahead query</h2>
      <dl className="kv">
        <dt>position</dt>
        <dd>
          <input
            type="range"
            min={0}
            max={Math.round(corridor.totalMiles)}
            step={1}
            value={truck.measure}
            onChange={(e) => onChange({ ...truck, measure: Number(e.target.value) })}
            style={{ width: '100%' }}
            aria-label="Truck position in corridor miles"
          />
        </dd>
        <dt />
        <dd className="mono">
          measure {truck.measure.toFixed(0)}
          {mp && ` (${mp})`}
        </dd>
        <dt>heading</dt>
        <dd>
          <select
            value={truck.direction}
            onChange={(e) =>
              onChange({ ...truck, direction: e.target.value as 'EB' | 'WB' })
            }
          >
            <option value="EB">EB</option>
            <option value="WB">WB</option>
          </select>
        </dd>
        <dt>look-ahead</dt>
        <dd>
          <select
            value={truck.lookAhead}
            onChange={(e) => onChange({ ...truck, lookAhead: Number(e.target.value) })}
          >
            {[5, 10, 25, 50, 100].map((d) => (
              <option key={d} value={d}>
                {d} mi
              </option>
            ))}
          </select>
        </dd>
      </dl>

      <div style={{ marginTop: 10 }}>
        {hits.length === 0 ? (
          <p className="empty">
            Nothing within {truck.lookAhead} mi {truck.direction}.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th className="num">mi</th>
                <th>class</th>
                <th className="num">conf</th>
                <th>agency</th>
              </tr>
            </thead>
            <tbody>
              {hits.map((h) => (
                <tr key={h.cl.clusterId}>
                  <td className="num mono">{h.dist.toFixed(1)}</td>
                  <td>
                    <i
                      className="swatch"
                      style={{ background: classColor(h.cl.eventClass) }}
                    />{' '}
                    {h.cl.eventClass}
                  </td>
                  <td className="num">{h.cl.confidence.value.toFixed(2)}</td>
                  <td className="row-sub">{h.cl.agencies.join(', ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="note">
        Ordered by distance. An opposing-direction event is excluded: for a truck heading
        east, a westbound closure is not its problem.
      </div>
    </div>
  );
}
