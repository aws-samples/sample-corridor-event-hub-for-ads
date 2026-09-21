/**
 * The record list: one row per current record, most recently touched first.
 *
 * A ROW HAS TO BE TRIAGEABLE WITHOUT OPENING IT. Class, state, where on the
 * corridor, who reported it, how many versions it has accumulated, how long since
 * anyone confirmed it, and how close it is to its TTL. The two conditions that mean
 * the pipeline may have failed the record - a lapsed TTL and an unresolved extent -
 * are marked on the row rather than only inside the trace, because otherwise finding
 * them means opening 100 traces.
 */

import { extentLabel, humanDuration, isSuspect, quietLevel, ttlProgress } from './derive';
import type { RecordRow } from './types';

function StateBadge({ state }: { state: string }) {
  return <span className={`state s-${state}`}>{state}</span>;
}

function Confidence({ value }: { value: number }) {
  return (
    <span className="conf" title={`confidence ${value.toFixed(4)} - open the trace for the breakdown`}>
      <i style={{ width: `${Math.round(value * 100)}%` }} />
      <b>{value.toFixed(2)}</b>
    </span>
  );
}

export function RecordList({
  rows,
  selected,
  onSelect,
}: {
  rows: RecordRow[];
  selected: string | null;
  onSelect: (eventId: string) => void;
}) {
  if (rows.length === 0) {
    return (
      <p className="empty">
        No records match. The filters above narrow an already-fetched list &mdash; widen the
        lifecycle states to include <span className="mono">cleared</span> and{' '}
        <span className="mono">merged</span> if you are looking for history rather than live
        records.
      </p>
    );
  }

  return (
    <div className="rows" role="list">
      {rows.map((row) => {
        const progress = ttlProgress(row);
        return (
          <button
            role="listitem"
            key={row.event_id}
            className={`row${selected === row.event_id ? ' on' : ''}${
              isSuspect(row) ? ' suspect' : ''
            }`}
            onClick={() => onSelect(row.event_id)}
          >
            <span className="row-head">
              <i className="swatch" style={{ background: `var(--c-${row.event_class})` }} />
              <span className="cls">{row.event_subtype || row.event_class}</span>
              <StateBadge state={row.lifecycle_state} />
              <span className="spacer" />
              <Confidence value={row.confidence} />
            </span>

            <span className="row-meta">
              <span className="mono">{extentLabel(row)}</span>
              <span className="dim">{row.direction}</span>
              <span className="dim">
                {row.agencies.length === 1 ? row.agencies[0] : `${row.agencies.length} agencies`}
              </span>
            </span>

            <span className="row-meta">
              <span className="dim">v{row.version}</span>
              <span className={`quiet ${quietLevel(row)}`}>
                quiet {humanDuration(row.quiet_seconds)}
              </span>
              {row.ttl_expired ? (
                <span className="flag bad" title="Past its TTL and not yet moved">
                  TTL lapsed
                </span>
              ) : progress !== null ? (
                <span className="ttl" title={`TTL expires ${row.ttl_expires_at}`}>
                  <i style={{ width: `${Math.round(progress)}%` }} />
                </span>
              ) : (
                <span className="dim" title="No TTL for this state, or one measured in years">
                  no timer
                </span>
              )}
              {row.unresolved_extent && (
                <span className="flag warn" title="No corridor measure: invisible to corridor queries">
                  unresolved
                </span>
              )}
              {row.related_event_ids.length > 0 && (
                <span className="flag" title={row.related_event_ids.join(', ')}>
                  {row.related_event_ids.length} linked
                </span>
              )}
              {row.independent_source_count > 1 && (
                <span className="flag good" title="Independent corroboration">
                  {row.independent_source_count} witnesses
                </span>
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}
