/**
 * The event timeline: where an event is in its life, and how much of it to believe.
 *
 * WHY THIS EXISTS. The strip answers "where on the corridor" and the confidence
 * breakdown answers "how good is the score". Neither answers the question a consumer
 * actually has to answer, which is temporal: this record was written by an agency at
 * one moment, changed at another, read by us at a third, and will time out at a
 * fourth - and the number attached to it is falling the whole time. Rendering
 * those as a list of ISO strings makes the reader do the subtraction; a snapshot with
 * a three-week-old confirmation looks identical to a live one until they do.
 *
 * THREE BANDS, and the split is the two clocks. Agency time (the event's own clock)
 * and system time (ours) are different clocks that agree only by coincidence, and the gap between
 * them is the trust signal. Timer marks are a third thing again: not observations at
 * all, but consequences scheduled by the state machine. Drawing all three on one lane
 * would imply they are the same kind of fact.
 *
 * WHAT IT REFUSES TO DRAW. A missing timestamp gets no mark - never a substituted one.
 * `historyAvailable: false` is rendered as prose rather than quietly presenting
 * `enteredAt` as when the event started, because this exporter re-observes everything
 * on every build (see NO_HISTORY_NOTE in strip_export.py). An honest gap is the point:
 * the timeline is only useful if its marks mean exactly what they say.
 */

import { useEffect, useState } from 'react';
import {
  DECAY_HORIZON_HALF_LIVES,
  EXAMPLE_TRUST_THRESHOLD,
  clipSpan,
  crossingSeconds,
  fmtDuration,
  fmtWhen,
  mergeNearbyMarks,
  projectConfidence,
  timelineDomain,
  timelineMarks,
  timelineSpans,
  trustSignals,
  worstGrade,
  type Grade,
  type TimelineMark,
  type TrustSignal,
} from './trust';
import { sourceName } from './sourceNames';
import type {
  ConfidenceModelOut,
  ConfidenceOut,
  LifecycleOut,
  StripCandidate,
  StripSource,
} from './types';

const W = 720;
const PAD_L = 8;
const PAD_R = 8;
const PLOT_W = W - PAD_L - PAD_R;

const AXIS_Y = 20;
const BAND_Y = { agency: 44, system: 76, timer: 104 };
const DECAY_Y = 128;
const DECAY_H = 44;
const H = DECAY_Y + DECAY_H + 30;

/** Ticks every ~1/4 of the window, snapped to nothing in particular: the window can
 * span minutes or years, so a calendar-aware tick generator would be a project of its
 * own for no gain at this size. Labels are relative, which is what a reader wants. */
const TICKS = 4;

function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

interface Props {
  members: StripCandidate[];
  sourcesById: Map<string, StripSource>;
  /** Null for a single source record: an adapter has no vocabulary for lifecycle
   *  state, so a candidate genuinely has none to show. */
  lifecycle: LifecycleOut | null;
  lifecycleState: string | null;
  confidence: ConfidenceOut;
  confidenceModel: ConfidenceModelOut;
  reviewPairCount: number;
  /** When the document was generated - the instant the confidence value was true. */
  generatedAt: string;
}

export function Timeline(props: Props) {
  const {
    members,
    sourcesById,
    lifecycle,
    lifecycleState,
    confidence,
    confidenceModel,
    reviewPairCount,
    generatedAt,
  } = props;

  // Own ticker rather than a prop: the marks are absolute instants, so the only thing
  // that moves is the "now" line and the projected value. Lifting that to App would
  // re-render the strip every second to animate one vertical rule.
  const nowMs = useNow();

  const input = { lifecycle, members, nowMs };
  const spans = timelineSpans(input, lifecycleState);
  // Marks and now set the window; spans are clipped into it. A multi-year work zone
  // would otherwise flatten the hour that matters. See timelineDomain.
  const observed = timelineMarks(input);
  // Ask for enough forward room that the decay curve shows its shape. timelineDomain
  // grants it only up to a bound - the observations keep at least half the axis.
  const halfLife = lifecycle?.confidenceHalfLifeSeconds ?? null;
  const scoredAt = Date.parse(generatedAt);
  const horizon =
    halfLife !== null && Number.isFinite(scoredAt)
      ? scoredAt + DECAY_HORIZON_HALF_LIVES * halfLife * 1000
      : undefined;
  const domain = timelineDomain(observed, nowMs, horizon);
  // Then merge what the resulting axis cannot separate. That is a legibility decision,
  // so it depends on the window and can only happen once the window is known.
  const marks = mergeNearbyMarks(observed, domain);

  const x = (ms: number) =>
    PAD_L + ((ms - domain.min) / (domain.max - domain.min)) * PLOT_W;

  const signals = trustSignals({
    members,
    sourcesById,
    lifecycle,
    reviewPairCount,
    nowMs,
  });

  const nowX = x(nowMs);
  const bands: Array<{ kind: TimelineMark['kind']; label: string; y: number }> = [
    { kind: 'agency', label: 'agency clock', y: BAND_Y.agency },
    { kind: 'system', label: 'our clock', y: BAND_Y.system },
    { kind: 'timer', label: 'scheduled', y: BAND_Y.timer },
  ];

  return (
    <div className="timeline">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Event timeline">
        {/* Axis: relative labels, because "in 4h" is the question and
            "2026-08-12T21:02Z" is homework. */}
        <line x1={PAD_L} y1={AXIS_Y} x2={W - PAD_R} y2={AXIS_Y} className="grid-line" />
        {Array.from({ length: TICKS + 1 }, (_, i) => {
          const t = domain.min + ((domain.max - domain.min) * i) / TICKS;
          return (
            <g key={i}>
              <line
                x1={x(t)}
                y1={AXIS_Y}
                x2={x(t)}
                y2={DECAY_Y + DECAY_H}
                className="state-line"
              />
              <text
                x={x(t)}
                y={AXIS_Y - 6}
                className="axis-text"
                textAnchor={i === 0 ? 'start' : i === TICKS ? 'end' : 'middle'}
              >
                {fmtWhen(t, nowMs)}
              </text>
            </g>
          );
        })}

        {bands.map((band) => (
          <text key={band.kind} x={PAD_L} y={band.y - 11} className="band-label">
            {band.label}
          </text>
        ))}

        {/* Spans first, so marks sit on top of them. */}
        {spans.map((s) => {
          const y = s.kind === 'agency' ? BAND_Y.agency : BAND_Y.timer;
          const clip = clipSpan(s, domain);
          const tip = `${s.label}\n${s.detail}\n${fmtWhen(s.from, nowMs)} → ${
            s.to === null ? 'open-ended' : fmtWhen(s.to, nowMs)
          }`;

          // A window that closed before this view opened, or opens after it ends. The
          // bar has nowhere to go, so the FACT goes in text at the edge it lies
          // beyond - an agency saying "this is over" must not become invisible just
          // because it said so outside the drawing window.
          if (clip.offscreen) {
            const past = s.to !== null && s.to < domain.min;
            return (
              <g key={s.key}>
                <text
                  x={past ? PAD_L + 10 : W - PAD_R - 10}
                  y={y + 2}
                  className="tl-span-offscreen"
                  textAnchor={past ? 'start' : 'end'}
                >
                  {past ? '←' : '→'} {s.label}{' '}
                  {past ? `ended ${fmtWhen(s.to!, nowMs)}` : `starts ${fmtWhen(s.from, nowMs)}`}
                  <title>{tip}</title>
                </text>
              </g>
            );
          }

          const x0 = x(clip.from);
          const x1 = clip.clippedRight ? W - PAD_R : x(clip.to);
          return (
            <g key={s.key}>
              <rect
                x={x0}
                y={y - 7}
                width={Math.max(2, x1 - x0)}
                height={11}
                className={`tl-span ${s.kind}${s.to === null ? ' open' : ''}`}
              >
                <title>{tip}</title>
              </rect>
              {/* Arrows mean "continues past this edge", never "ends here". */}
              {clip.clippedLeft && (
                <text x={PAD_L + 1} y={y + 3} className="tl-span-cap">
                  ←
                </text>
              )}
              {clip.clippedRight && (
                <text x={W - PAD_R - 1} y={y + 3} className="tl-span-cap" textAnchor="end">
                  →
                </text>
              )}
              <text x={x0 + (clip.clippedLeft ? 11 : 3)} y={y + 2} className="tl-span-label">
                {s.label}
              </text>
            </g>
          );
        })}

        {marks.map((m, i) => {
          const y = BAND_Y[m.kind];
          const mx = x(m.at);
          // Alternate the label side so two near-simultaneous marks (the usual case -
          // we fetch every feed in one pass) do not overprint each other.
          const above = i % 2 === 0;
          return (
            <g key={m.key} className={`tl-mark ${m.grade ?? ''}`}>
              <line x1={mx} y1={y - 9} x2={mx} y2={y + 9} className="tl-mark-line" />
              <circle cx={mx} cy={y} r={3.5} className="tl-mark-dot" />
              <text
                x={mx + 5}
                y={above ? y - 11 : y + 15}
                className="tl-mark-label"
                textAnchor={mx > W * 0.7 ? 'end' : 'start'}
                dx={mx > W * 0.7 ? -9 : 0}
              >
                {m.label}
              </text>
              <title>{`${m.label}\n${fmtWhen(m.at, nowMs)}\n${m.detail}`}</title>
              {/* A slightly larger dot for a mark standing in for several records, so
                  a collapsed group does not read as a single event. */}
              {m.count > 1 && <circle cx={mx} cy={y} r={6} className="tl-mark-group" />}
            </g>
          );
        })}

        <DecayCurve
          confidence={confidence}
          confidenceModel={confidenceModel}
          halfLifeSeconds={halfLife}
          scoredAt={scoredAt}
          domain={domain}
          x={x}
          nowMs={nowMs}
        />

        {/* NOW, drawn last so it is never hidden behind a bar. */}
        <line x1={nowX} y1={AXIS_Y} x2={nowX} y2={DECAY_Y + DECAY_H} className="tl-now" />
        <text x={nowX + 4} y={DECAY_Y + DECAY_H + 12} className="tl-now-label">
          now
        </text>
      </svg>

      <TrustChips signals={signals} />

      {lifecycle && !lifecycle.historyAvailable && (
        <div className="note tl-caveat">
          <b>No lifecycle history.</b> {lifecycle.note}
        </div>
      )}
    </div>
  );
}

interface DecayProps {
  confidence: ConfidenceOut;
  confidenceModel: ConfidenceModelOut;
  /** Null when the selection has no lifecycle profile, so no decay law either. */
  halfLifeSeconds: number | null;
  /** When the confidence value was computed. Passed in rather than re-derived: the
   *  parent already needed it to size the axis, and two derivations of one instant is
   *  one too many. */
  scoredAt: number;
  domain: { min: number; max: number };
  x: (ms: number) => number;
  nowMs: number;
}

/**
 * Confidence over time, forward from when it was scored.
 *
 * This is the part that turns a number into a claim someone can act on. The curve is
 * not a guess: recency decays on a published class half-life and every other component
 * is constant, so given the weights the future value is arithmetic. The floor is drawn
 * because it is the non-obvious half of the story - decay does not reach zero, it
 * settles at whatever the score does not owe to recency, and an event can therefore
 * sit above a threshold indefinitely.
 */
function DecayCurve(props: DecayProps) {
  const { confidence, confidenceModel, halfLifeSeconds, scoredAt, domain, x, nowMs } = props;

  const halfLife = halfLifeSeconds;
  const recencyWeight = confidenceModel.weights.recency;

  if (halfLife === null || recencyWeight === undefined || !Number.isFinite(scoredAt)) {
    return null;
  }

  const y = (value: number) => DECAY_Y + DECAY_H - value * DECAY_H;
  const ahead = (ms: number) => (ms - scoredAt) / 1000;

  // THE CURVE STARTS WHERE THE SCORE WAS TAKEN, not at the left edge of the axis. The
  // window usually reaches weeks back to cover the agency's edits, and there is no
  // historical score to plot there - only recency has a law describing how it changed,
  // while corroboration and completeness moved as sources arrived. Sampling the whole
  // axis drew a flat line pinned at 1.00 across all of that: a past in which the event
  // was perfectly trusted, invented by a clamp.
  const from = Math.max(domain.min, scoredAt);
  if (from >= domain.max) return null;

  // 48 samples: the curve is smooth and this is 720px wide, so more is invisible.
  const points = Array.from({ length: 49 }, (_, i) => {
    const t = from + ((domain.max - from) * i) / 48;
    return `${x(t).toFixed(1)},${y(projectConfidence(confidence, recencyWeight, halfLife, ahead(t))).toFixed(1)}`;
  }).join(' ');

  const nowValue = projectConfidence(confidence, recencyWeight, halfLife, ahead(nowMs));
  const floor = Math.max(0, confidence.value - recencyWeight * confidence.breakdown.recency);
  const crossing = crossingSeconds(
    confidence,
    recencyWeight,
    halfLife,
    EXAMPLE_TRUST_THRESHOLD,
  );
  const crossingMs = crossing === null ? null : scoredAt + crossing * 1000;

  return (
    <g>
      <text x={PAD_L} y={DECAY_Y - 4} className="band-label">
        confidence, projected (half-life {fmtDuration(halfLife)})
      </text>
      <rect x={PAD_L} y={DECAY_Y} width={PLOT_W} height={DECAY_H} className="tl-decay-bg" />

      {/* The floor: what the score keeps regardless of how long the silence runs. */}
      <line
        x1={PAD_L}
        y1={y(floor)}
        x2={W - PAD_R}
        y2={y(floor)}
        className="tl-decay-floor"
      />
      <text x={W - PAD_R - 2} y={y(floor) - 3} className="tl-decay-tick" textAnchor="end">
        floor {floor.toFixed(2)} (does not age)
      </text>

      {/* Example threshold, plus where the curve meets it. */}
      <line
        x1={PAD_L}
        y1={y(EXAMPLE_TRUST_THRESHOLD)}
        x2={W - PAD_R}
        y2={y(EXAMPLE_TRUST_THRESHOLD)}
        className="tl-decay-threshold"
      />
      <text x={PAD_L + 2} y={y(EXAMPLE_TRUST_THRESHOLD) - 3} className="tl-decay-tick">
        example threshold {EXAMPLE_TRUST_THRESHOLD.toFixed(2)}
      </text>

      <polyline points={points} className="tl-decay-line" />
      {/* Where the projection begins, marked so the empty left-hand region reads as
          "not computed" rather than as an axis the curve failed to reach. */}
      {from > domain.min && (
        <>
          <line x1={x(from)} y1={DECAY_Y} x2={x(from)} y2={DECAY_Y + DECAY_H} className="tl-decay-start" />
          <text x={x(from) - 3} y={DECAY_Y + DECAY_H - 4} className="tl-decay-tick" textAnchor="end">
            no score before here
          </text>
        </>
      )}

      {crossingMs !== null && crossingMs >= domain.min && crossingMs <= domain.max && (
        <g>
          <circle
            cx={x(crossingMs)}
            cy={y(EXAMPLE_TRUST_THRESHOLD)}
            r={3.5}
            className="tl-decay-cross"
          />
          <text
            x={x(crossingMs) + 5}
            y={y(EXAMPLE_TRUST_THRESHOLD) + 12}
            className="tl-decay-tick warn"
          >
            crosses {fmtWhen(crossingMs, nowMs)}
          </text>
        </g>
      )}

      <circle cx={x(nowMs)} cy={y(nowValue)} r={3.5} className="tl-decay-now" />
      <text x={x(nowMs) + 6} y={y(nowValue) - 5} className="tl-decay-value">
        {nowValue.toFixed(3)} now
      </text>
    </g>
  );
}

const GRADE_WORD: Record<Grade, string> = {
  good: 'ok',
  warn: 'caution',
  bad: 'do not rely',
  unknown: 'unknown',
};

function TrustChips({ signals }: { signals: TrustSignal[] }) {
  if (signals.length === 0) return null;
  const worst = worstGrade(signals);
  return (
    <div className="trust">
      <div className="trust-head">
        <span className={`pill grade-${worst}`}>{GRADE_WORD[worst]}</span>
        <span className="row-sub-text">
          worst of {signals.length} signals &mdash; the ceiling on how far this event
          should be trusted
        </span>
      </div>
      <div className="trust-chips">
        {signals.map((s) => (
          // The `why` is a title rather than always-visible prose: nine chips of
          // running explanation would bury the strip below the fold. The VALUE is
          // never hidden, only the reasoning is one hover away.
          <div key={s.key} className={`chip grade-${s.grade}`} title={s.why}>
            <span className="chip-label">{s.label}</span>
            <span className="chip-value">{s.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * The state machine, rendered from the served table rather than a copy.
 * Sits beside the timeline: the marks show when the timer fires, this shows what it
 * does, and neither is much use without the other.
 */
export function LifecycleNext({
  lifecycle,
  state,
  ttlSeconds,
}: {
  lifecycle: LifecycleOut;
  state: string;
  ttlSeconds: number | null;
}) {
  return (
    <div className="lifecycle-next">
      <h2>
        Lifecycle &mdash; from <span className="mono">{state}</span>
      </h2>
      <table>
        <thead>
          <tr>
            <th>to state</th>
            <th>on trigger</th>
          </tr>
        </thead>
        <tbody>
          {lifecycle.transitions.map((t) => (
            <tr key={t.toState}>
              <td className="mono">{t.toState}</td>
              <td className="row-sub" title={t.rationale}>
                {t.triggers.join(', ')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <dl className="kv" style={{ marginTop: 10 }}>
        <dt>ttl</dt>
        <dd>
          {ttlSeconds === null ? (
            <span className="empty">none for this state</span>
          ) : (
            fmtDuration(ttlSeconds)
          )}
        </dd>
        <dt>re-open window</dt>
        <dd title="A recurrence inside this window re-opens the same event rather than creating a new one.">
          {fmtDuration(lifecycle.reopenWindowSeconds)}
        </dd>
      </dl>

      <h2 style={{ marginTop: 14 }}>If a feed stops reporting it</h2>
      <table>
        <thead>
          <tr>
            <th>source</th>
            <th>semantics</th>
            <th>goes to</th>
          </tr>
        </thead>
        <tbody>
          {lifecycle.sourceAbsent.map((s) => (
            <tr key={s.sourceId}>
              <td title={s.sourceId}>{sourceName(s.sourceId)}</td>
              <td className="row-sub">{s.snapshotSemantics ?? 'unknown'}</td>
              <td>
                <span className={s.toState === 'cleared' ? undefined : 'warn-inline'}>
                  {s.toState}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="note">
        A record leaving an agency&rsquo;s snapshot means <i>cleared</i> for some feeds and
        <i> unknown</i> for others, and it cannot be inferred from the data &mdash; it is one
        phone call per DOT. Until an agency confirms, disappearance routes to{' '}
        <span className="mono">clearing</span>: lingering too long is recoverable, clearing a
        live hazard in front of an automated truck is not.
      </div>
    </div>
  );
}
