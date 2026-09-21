/**
 * The selection panel: what a click on the strip resolves to.
 *
 * Sits directly under the chart, in two columns, so a click resolves where the eye
 * already is rather than across the page.
 *
 * This is where the deck's problems 2 and 3 become concrete. A merge that cannot
 * say WHY it merged is indistinguishable from a bug, and a confidence
 * value without its breakdown is an opaque number that an integrator cannot set a
 * trust threshold against. Both are rendered in full.
 *
 * The timeline above them adds the third question, which is temporal: the breakdown
 * explains the score AT AN INSTANT, and recency has been decaying since. See
 * Timeline.tsx.
 */

import { Timeline, LifecycleNext } from './Timeline';
import { clusterMembers, fmtDuration, fmtWhen } from './trust';
import type {
  ConfidenceModelOut,
  ConfidenceOut,
  MatchPairOut,
  StripCandidate,
  StripCluster,
  StripSource,
} from './types';
import type { Selection } from './Strip';

function ExplainList({ lines }: { lines: string[] }) {
  return (
    <ul className="explain">
      {lines.map((l, i) => (
        <li
          key={i}
          className={
            /^total:/.test(l) ? 'total' : /GATE FAILED/.test(l) ? 'gate' : undefined
          }
        >
          {l}
        </li>
      ))}
    </ul>
  );
}

function Meter({ value }: { value: number }) {
  return (
    <div className="bar-meter">
      <div style={{ width: `${(value * 100).toFixed(0)}%` }} />
    </div>
  );
}

function Confidence({ c, title }: { c: ConfidenceOut; title: string }) {
  return (
    <>
      <h2>
        {title} {c.value.toFixed(3)}
      </h2>
      <Meter value={c.value} />
      <div style={{ marginTop: 8 }}>
        <ExplainList lines={c.explanation} />
      </div>
    </>
  );
}

function PairList({ pairs, kind }: { pairs: MatchPairOut[]; kind: 'join' | 'review' }) {
  return (
    <>
      {pairs.map((p, i) => (
        <div key={i} style={{ marginTop: 6 }}>
          <b className="mono">
            #{p.from} {kind === 'join' ? '+' : 'vs'} #{p.to}
          </b>{' '}
          &rarr; {p.value.toFixed(3)}
          {kind === 'review' && (
            <span style={{ color: 'var(--warn)' }}> not merged, sent to review</span>
          )}
          <ExplainList lines={p.explanation} />
        </div>
      ))}
    </>
  );
}

interface Props {
  selection: Selection | null;
  candidates: Map<number, StripCandidate>;
  clusters: StripCluster[];
  sources: StripSource[];
  confidenceModel: ConfidenceModelOut;
  /** When the selected confidence value was computed - the timeline projects from it. */
  generatedAt: string;
}

export function DetailPanel(props: Props) {
  const { selection, candidates, clusters, sources, confidenceModel, generatedAt } = props;
  const sourcesById = new Map(sources.map((s) => [s.sourceId, s]));
  // Ages in the tables are relative to the SNAPSHOT, not to the wall clock. The
  // timeline animates against now; a table row that silently aged while the document
  // behind it did not would be a third, unlabelled clock.
  const snapshotMs = Date.parse(generatedAt);

  if (!selection) {
    return (
      <div className="card">
        <h2>Selection</h2>
        <p className="empty">Click a bar on the strip.</p>
      </div>
    );
  }

  if (selection.kind === 'candidate') {
    const c = candidates.get(selection.id);
    if (!c) {
      return (
        <div className="card">
          <h2>Selection</h2>
          <p className="empty">That record is no longer in the current data.</p>
        </div>
      );
    }
    return (
      <div className="card">
        <h2>Source record</h2>
        {/* No lifecycle for a single report: an adapter emits candidates and has no
            vocabulary for lifecycle state, so this timeline shows the record's
            own clocks and nothing about state. The MERGED row is where an event has a
            life. */}
        <Timeline
          members={[c]}
          sourcesById={sourcesById}
          lifecycle={null}
          lifecycleState={null}
          confidence={c.confidence}
          confidenceModel={confidenceModel}
          reviewPairCount={0}
          generatedAt={generatedAt}
        />
        <div className="detail-cols" style={{ marginTop: 14 }}>
          <div>
            <dl className="kv">
              <dt>class</dt>
              <dd>
                {c.eventClass} / {c.eventSubtype}
              </dd>
              <dt>agency</dt>
              <dd>{c.agency}</dd>
              <dt>native id</dt>
              <dd className="mono">{c.nativeId}</dd>
              <dt>extent</dt>
              <dd>
                {c.beginLabel} &rarr; {c.endLabel} ({c.direction})
              </dd>
              <dt>measure</dt>
              <dd className="mono">
                {c.beginMeasure.toFixed(1)} &ndash; {c.endMeasure.toFixed(1)}
              </dd>
              <dt>conflation</dt>
              <dd>
                {c.conflationMethod}
                {c.positionalAccuracyMeters !== null &&
                  ` · ±${Math.round(c.positionalAccuracyMeters)}m`}
              </dd>
              <dt>time</dt>
              <dd>
                {c.startTime}
                <br />
                {c.endTime ?? 'OPEN-ENDED'} ({c.timeConfidence})
              </dd>
              {/* Both clocks, labelled. The agency's edit time and our fetch
                  time are different facts, and only the first one decays confidence. */}
              <dt>agency changed</dt>
              <dd>
                {c.sourceUpdatedAt ?? (
                  <span className="empty">not reported by this feed</span>
                )}
              </dd>
              <dt>we fetched</dt>
              <dd>{c.retrievedAt}</dd>
              <dt>lanes</dt>
              <dd>
                {c.laneImpacts.length === 0 ? (
                  <span className="empty">none reported</span>
                ) : (
                  c.laneImpacts.map((l) => (
                    <div key={l.ordinal}>
                      lane {l.ordinal} ({l.type}): {l.status}
                      {l.inferred && <i> inferred</i>}
                    </div>
                  ))
                )}
              </dd>
              <dt>agency severity</dt>
              <dd>{c.agencySeverity ?? '–'}</dd>
              <dt>raw bytes</dt>
              <dd className="mono" style={{ fontSize: 10 }}>
                {c.rawRef}
              </dd>
            </dl>
          </div>
          <div>
            <Confidence c={c.confidence} title="Confidence" />
            <div className="note">
              Report-level score: this source alone, uncorroborated. Select the bar on the
              MERGED row for the event-level score.
            </div>
          </div>
        </div>
      </div>
    );
  }

  const cl = clusters[selection.id];
  if (!cl) {
    return (
      <div className="card">
        <h2>Selection</h2>
        <p className="empty">That event is no longer in the current data.</p>
      </div>
    );
  }
  const members = clusterMembers(cl, candidates);

  return (
    <div className="card">
      <h2>Merged event #{cl.clusterId}</h2>

      <Timeline
        members={members}
        sourcesById={sourcesById}
        lifecycle={cl.lifecycle}
        lifecycleState={cl.lifecycleState}
        confidence={cl.confidence}
        confidenceModel={confidenceModel}
        reviewPairCount={cl.reviewPairs.length}
        generatedAt={generatedAt}
      />

      <div className="detail-cols" style={{ marginTop: 14 }}>
        <div>
          <dl className="kv">
            <dt>class</dt>
            <dd>{cl.eventClass}</dd>
            <dt>lifecycle</dt>
            <dd>
              {cl.lifecycleState}
              {cl.ttlSeconds !== null && (
                <span className="row-sub"> (TTL {fmtDuration(cl.ttlSeconds)})</span>
              )}
            </dd>
            <dt>last confirmed</dt>
            <dd title="The freshest agency update across the cluster. Confidence decays from here, not from when we fetched.">
              {fmtWhen(Date.parse(cl.lifecycle.lastConfirmedAt), snapshotMs)}
              <span className="row-sub"> as of this snapshot</span>
            </dd>
            <dt>extent</dt>
            <dd className="mono">
              {cl.beginMeasure.toFixed(1)} &ndash; {cl.endMeasure.toFixed(1)} ({cl.direction})
            </dd>
            <dt>agencies</dt>
            <dd>{cl.agencies.join(', ')}</dd>
          </dl>

          <h2 style={{ marginTop: 14 }}>Contributing records</h2>
          <table>
            <thead>
              <tr>
                <th>agency</th>
                <th>native id</th>
                <th>changed</th>
                <th className="num">conf</th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.id}>
                  <td>{m.agency}</td>
                  <td className="mono">{m.nativeId}</td>
                  {/* Per-record, because a cluster is only as fresh as its freshest
                      member and this is where a stale corroborator becomes visible. */}
                  <td className="row-sub">
                    {m.sourceUpdatedAt
                      ? fmtWhen(Date.parse(m.sourceUpdatedAt), snapshotMs)
                      : 'not reported'}
                  </td>
                  <td className="num">{m.confidence.value.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div style={{ marginTop: 14 }}>
            <Confidence c={cl.confidence} title="Confidence" />
          </div>
        </div>

        <div>
          <h2>Match decisions</h2>
          {cl.joins.length === 0 ? (
            <p className="empty">Single source &mdash; nothing merged into this event.</p>
          ) : (
            <PairList pairs={cl.joins} kind="join" />
          )}
          {cl.reviewPairs.length > 0 && <PairList pairs={cl.reviewPairs} kind="review" />}

          <div style={{ marginTop: 14 }}>
            <LifecycleNext
              lifecycle={cl.lifecycle}
              state={cl.lifecycleState}
              ttlSeconds={cl.ttlSeconds}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
