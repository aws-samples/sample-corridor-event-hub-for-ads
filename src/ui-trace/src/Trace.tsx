/**
 * One record's life, from ingestion to whatever ends it.
 *
 * FOUR LAYERS, COARSEST FIRST, because that is the order the questions arrive in:
 *
 *   1. Findings      is anything wrong with this record?
 *   2. Stages        how far did it get, and what is the evidence at each stage?
 *   3. State band    where has it spent its life, proportionally?
 *   4. Steps         every recorded transition, its trigger, its reason, its diff,
 *                    and the exact bytes that caused it.
 *
 * The steps are the ground truth and everything above them is derived from the same
 * document, so the summary can never say something the detail contradicts.
 *
 * CONFIRMATION RUNS COLLAPSE. A record polled every 60 seconds accumulates hundreds
 * of steps that changed nothing but `raw_ref`, and rendering them one per row hides
 * the two that moved it. They collapse into a counted run that expands on click -
 * the count itself is a trust signal ("confirmed 1,563 times") rather than noise to
 * discard.
 */

import { useMemo, useState } from 'react';
import {
  clockTime,
  extentLabel,
  groupSteps,
  humanDuration,
  refLabel,
  sortChanges,
  sortFindings,
  stateBands,
  visibleChanges,
} from './derive';
import type { Change, Finding, Meta, Stage, Step, Trace as TraceDoc } from './types';

function Findings({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return (
      <p className="ok-line">
        No findings: the audit trail is contiguous, the timer is on schedule, and the extent
        resolved.
      </p>
    );
  }
  return (
    <div className="findings">
      {sortFindings(findings).map((finding, index) => (
        <div key={`${finding.code}-${index}`} className={`finding ${finding.severity}`}>
          <span className="code mono">{finding.code}</span>
          <span className="detail">{finding.detail}</span>
        </div>
      ))}
    </div>
  );
}

function Evidence({ evidence }: { evidence: Record<string, unknown> }) {
  const entries = Object.entries(evidence).filter(
    ([, value]) => value !== null && value !== undefined && !(Array.isArray(value) && value.length === 0),
  );
  if (entries.length === 0) return null;
  return (
    <dl className="evidence">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt className="mono">{key}</dt>
          <dd>{renderValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (Array.isArray(value)) return value.map((v) => renderValue(v)).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function Stages({ stages }: { stages: Stage[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="stages">
      {stages.map((stage) => (
        <div key={stage.stage} className={`stage ${stage.status}`}>
          <button className="stage-head" onClick={() => setOpen(open === stage.stage ? null : stage.stage)}>
            <span className="dot" />
            <span className="label">{stage.label}</span>
            <span className="at mono">{stage.at ? clockTime(stage.at) : 'not reached'}</span>
          </button>
          <ul className="stage-detail">
            {stage.detail.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {open === stage.stage && (
            <div className="stage-more">
              <p className="what">{stage.what}</p>
              <Evidence evidence={stage.evidence} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function StateBand({ doc }: { doc: TraceDoc }) {
  const bands = useMemo(() => stateBands(doc.states), [doc.states]);
  if (bands.length === 0) return null;
  return (
    <div className="band-wrap">
      <div className="band">
        {bands.map((band, index) => (
          <div
            key={`${band.span.state}-${index}`}
            className={`seg s-${band.span.state}${band.span.current ? ' current' : ''}`}
            style={{ width: `${band.percent}%` }}
            title={
              `${band.span.state}: ${humanDuration(band.span.seconds)}` +
              (band.span.updates > 0 ? `, ${band.span.updates} confirmation(s)` : '') +
              `\nentered ${band.span.entered_at}` +
              (band.span.unaudited ? '\nNO AUDIT RECORD EXPLAINS THIS STATE' : '')
            }
          >
            {/* A label wider than its segment is worse than no label: it clips
                mid-word and reads as a different state ("alidated"). Below ~12% there
                is no room for one, and the tooltip carries the whole story. */}
            {band.percent >= 12 && (
              <span className="seg-label">
                {band.span.state} <b>{humanDuration(band.span.seconds)}</b>
                {band.span.updates > 0 && <i> &middot;{band.span.updates}x</i>}
              </span>
            )}
          </div>
        ))}
      </div>
      <p className="hint">
        Widths are proportional to time in state, with a floor so a state occupied for seconds is
        still visible. <b>&middot;Nx</b> is how many times a feed re-confirmed the record without
        moving it.
      </p>
    </div>
  );
}

function Diff({ changes, showBookkeeping }: { changes: Change[]; showBookkeeping: boolean }) {
  const { shown, hidden } = visibleChanges(changes, showBookkeeping);
  if (shown.length === 0 && hidden === 0) return null;
  return (
    <div className="diff">
      {sortChanges(shown).map((change) => (
        <div key={change.path} className={`change${change.notable ? ' notable' : ''}`}>
          <span className="path mono">{change.path}</span>
          <span className="from mono">{renderValue(change.from)}</span>
          <span className="arrow">&rarr;</span>
          <span className="to mono">{renderValue(change.to)}</span>
        </div>
      ))}
      {hidden > 0 && (
        <div className="change hidden-note">
          {hidden} re-fetch change(s) hidden &mdash; payload pointer, fetch time, and the
          clock-driven part of confidence
        </div>
      )}
    </div>
  );
}

function StepRow({
  step,
  showBookkeeping,
  onOpenRaw,
}: {
  step: Step;
  showBookkeeping: boolean;
  onOpenRaw: (ref: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const moved = step.transition;
  return (
    <div className={`step${moved ? ' moved' : ''}${step.legal ? '' : ' illegal'}`}>
      <button className="step-head" onClick={() => setOpen(!open)}>
        <span className="seq mono">#{step.sequence}</span>
        <span className="when mono">{clockTime(step.recorded_at)}</span>
        <span className="move">
          {moved ? (
            <>
              <span className={`state s-${step.from_state}`}>{step.from_state}</span>
              <span className="arrow">&rarr;</span>
              <span className={`state s-${step.to_state}`}>{step.to_state}</span>
            </>
          ) : (
            <span className="confirm">
              confirmed <span className={`state s-${step.to_state}`}>{step.to_state}</span>
            </span>
          )}
        </span>
        <span className="trigger mono">
          {step.trigger}/{step.actor}
        </span>
        {step.confidence !== null && <span className="conf-num mono">{step.confidence.toFixed(2)}</span>}
        {!step.legal && <span className="flag bad">not in the transition table</span>}
        {step.version_missing && <span className="flag bad">no version stored</span>}
      </button>

      <div className="step-body">
        <p className="reason">{step.reason}</p>
        <Diff changes={step.changes} showBookkeeping={showBookkeeping} />
        {open && (
          <dl className="evidence">
            <div>
              <dt className="mono">occurred_at</dt>
              <dd className="mono">{step.occurred_at || '-'}</dd>
            </div>
            <div>
              <dt className="mono">recorded_at</dt>
              <dd className="mono">
                {step.recorded_at || '-'}
                {step.lag_seconds !== null && ` (+${step.lag_seconds}s)`}
              </dd>
            </div>
            <div>
              <dt className="mono">rule_version</dt>
              <dd className="mono">{step.rule_version}</dd>
            </div>
            {step.operator_id && (
              <div>
                <dt className="mono">operator_id</dt>
                <dd className="mono">{step.operator_id}</dd>
              </div>
            )}
            <div>
              <dt className="mono">version</dt>
              <dd className="mono">{step.version ?? '-'}</dd>
            </div>
          </dl>
        )}
        {step.payload_ref && (
          <button className="link" onClick={() => onOpenRaw(step.payload_ref as string)}>
            raw payload: {refLabel(step.payload_ref)}
          </button>
        )}
      </div>
    </div>
  );
}

function Steps({
  doc,
  onOpenRaw,
}: {
  doc: TraceDoc;
  onOpenRaw: (ref: string) => void;
}) {
  const [showBookkeeping, setShowBookkeeping] = useState(false);
  const [newestFirst, setNewestFirst] = useState(true);
  const [expandedRuns, setExpandedRuns] = useState<Record<number, boolean>>({});

  const groups = useMemo(() => {
    const ordered = newestFirst ? [...doc.steps].reverse() : doc.steps;
    return groupSteps(ordered);
  }, [doc.steps, newestFirst]);

  return (
    <div className="card">
      <h2>
        Steps &mdash; every recorded transition ({doc.counts.audit})
        <span className="spacer" />
        <label className="chk">
          <input
            type="checkbox"
            checked={showBookkeeping}
            onChange={(e) => setShowBookkeeping(e.target.checked)}
          />{' '}
          show re-fetch changes
        </label>
        <button className="link" onClick={() => setNewestFirst(!newestFirst)}>
          {newestFirst ? 'newest first' : 'oldest first'}
        </button>
      </h2>

      {doc.window.windowed && (
        <div className="banner">
          <b>Windowed read.</b> {doc.window.note}. Raise it with{' '}
          <span className="mono">?window=1000</span> on the API if you need more.
        </div>
      )}

      <div className="steps">
        {groups.map((group, index) => {
          if (group.kind === 'step') {
            return (
              <StepRow
                key={`s-${group.step.sequence}`}
                step={group.step}
                showBookkeeping={showBookkeeping}
                onOpenRaw={onOpenRaw}
              />
            );
          }
          const expanded = expandedRuns[index] ?? false;
          return (
            <div key={`r-${index}`} className="run">
              <button
                className="run-head"
                onClick={() => setExpandedRuns({ ...expandedRuns, [index]: !expanded })}
              >
                <span className="count">{group.count}x</span>
                <span>
                  re-confirmed <span className={`state s-${group.state}`}>{group.state}</span> over{' '}
                  {humanDuration(group.seconds)} with no change to the record
                </span>
                <span className="spacer" />
                <span className="mono dim">
                  #{Math.min(group.first.sequence, group.last.sequence)}&ndash;
                  {Math.max(group.first.sequence, group.last.sequence)}
                </span>
                <span className="chev">{expanded ? '−' : '+'}</span>
              </button>
              {expanded && (
                <div className="run-body">
                  <StepRow
                    step={group.first}
                    showBookkeeping={showBookkeeping}
                    onOpenRaw={onOpenRaw}
                  />
                  <p className="hint">
                    &hellip;{group.count - 2 > 0 ? `${group.count - 2} more like this` : ''}&hellip;
                  </p>
                  <StepRow
                    step={group.last}
                    showBookkeeping={showBookkeeping}
                    onOpenRaw={onOpenRaw}
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Provenance({ doc }: { doc: TraceDoc }) {
  const provenance = Object.entries(doc.field_provenance);
  const alternates = Object.entries(doc.alternates);
  return (
    <div className="card">
      <h2>Sources and provenance</h2>
      <table className="tbl">
        <thead>
          <tr>
            <th>source</th>
            <th>agency record id</th>
            <th>agency said</th>
            <th>we fetched</th>
            <th>contributed</th>
          </tr>
        </thead>
        <tbody>
          {doc.sources.map((source) => (
            <tr key={`${source.source_id}-${source.native_id}`}>
              <td className="mono">{source.source_id}</td>
              <td className="mono">{source.native_id}</td>
              <td className="mono">{source.source_updated_at ?? 'not stated'}</td>
              <td className="mono">{source.retrieved_at}</td>
              <td>{source.contributed_fields.join(', ') || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {provenance.length > 0 && (
        <>
          <h3>Which source won each field</h3>
          <dl className="evidence">
            {provenance.map(([field, value]) => (
              <div key={field}>
                <dt className="mono">{field}</dt>
                <dd className="mono">{renderValue(value)}</dd>
              </div>
            ))}
          </dl>
        </>
      )}

      {alternates.length > 0 && (
        <>
          <h3>What the losing sources said, retained</h3>
          <dl className="evidence">
            {alternates.map(([field, value]) => (
              <div key={field}>
                <dt className="mono">{field}</dt>
                <dd className="mono">{renderValue(value)}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </div>
  );
}

export function Trace({
  doc,
  meta,
  onOpenRaw,
  onSelect,
}: {
  doc: TraceDoc;
  meta: Meta | null;
  onOpenRaw: (ref: string, nativeId?: string | null) => void;
  onSelect: (eventId: string) => void;
}) {
  const row = doc.summary;
  const nativeId = doc.sources[0]?.native_id ?? null;
  const ladderNext = meta?.ttl_ladder[row.lifecycle_state];

  return (
    <div className="trace">
      <div className="card">
        <h2>
          <span className={`state s-${row.lifecycle_state}`}>{row.lifecycle_state}</span>
          <span className="mono id">{doc.event_id}</span>
          <span className="spacer" />
          <span className="dim">
            {row.event_class} / {row.event_subtype} &middot; v{row.version} &middot;{' '}
            {doc.counts.versions} versions &middot; {doc.counts.payload_refs} payload(s)
          </span>
        </h2>

        <p className="lede">
          {extentLabel(row)} &middot; {row.direction} &middot; first seen{' '}
          {humanDuration(row.age_seconds)} ago &middot; last confirmed{' '}
          {humanDuration(row.quiet_seconds)} ago
          {row.ttl_expires_at && !row.terminal && (
            <>
              {' '}
              &middot; TTL{' '}
              {row.ttl_expired ? (
                <b className="bad">lapsed</b>
              ) : (
                <>in {humanDuration(row.seconds_until_ttl)}</>
              )}
              {ladderNext && <> &rarr; {ladderNext}</>}
            </>
          )}
        </p>

        <Findings findings={doc.findings} />
      </div>

      <div className="card">
        <h2>Pipeline stages</h2>
        <Stages stages={doc.stages} />
      </div>

      <div className="card">
        <h2>Time in each state</h2>
        <StateBand doc={doc} />
      </div>

      <Steps doc={doc} onOpenRaw={(ref) => onOpenRaw(ref, nativeId)} />

      <Provenance doc={doc} />

      {doc.related.length > 0 && (
        <div className="card">
          <h2>Linked records (merges, un-merges, secondary events)</h2>
          <div className="related">
            {doc.related.map((other) =>
              other.missing ? (
                <p key={other.event_id} className="warn-line">
                  <span className="mono">{other.event_id}</span> is referenced but not in the store.
                </p>
              ) : (
                <button key={other.event_id} className="row" onClick={() => onSelect(other.event_id)}>
                  <span className="row-head">
                    <i className="swatch" style={{ background: `var(--c-${other.event_class})` }} />
                    <span className="cls">{other.event_subtype || other.event_class}</span>
                    <span className={`state s-${other.lifecycle_state}`}>
                      {other.lifecycle_state}
                    </span>
                    <span className="spacer" />
                    <span className="mono dim">{other.event_id}</span>
                  </span>
                  <span className="row-meta">
                    <span className="mono">{extentLabel(other)}</span>
                    <span className="dim">{other.agencies.join(', ')}</span>
                  </span>
                </button>
              ),
            )}
          </div>
        </div>
      )}

      {Object.keys(doc.extensions).length > 0 && (
        <div className="card">
          <h2>Everything the source said that has no canonical home</h2>
          <dl className="evidence">
            {Object.entries(doc.extensions).map(([key, value]) => (
              <div key={key}>
                <dt className="mono">{key}</dt>
                <dd>{renderValue(value)}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </div>
  );
}
