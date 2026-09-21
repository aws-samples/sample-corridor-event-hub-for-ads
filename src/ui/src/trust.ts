/**
 * Event timeline geometry and trustworthiness grading. No React, no DOM - all of it
 * is arithmetic over the wire document, so all of it is testable (see trust.test.ts).
 *
 * TWO THINGS THIS MODULE EXISTS TO PREVENT.
 *
 * 1. A CONFIDENCE NUMBER THAT LOOKS LIKE A CONSTANT. Recency decays continuously on
 *    a class half-life, so the 0.71 in the panel is only true for the instant
 *    it was computed. A truck deciding whether to trust a record about a hazard
 *    beyond its sensors needs to know the number is falling and roughly when it
 *    stops being usable. `projectConfidence` and `crossingSeconds` answer that from
 *    the published weights rather than from a guess - which is why the export now
 *    ships `confidenceModel.weights` instead of this file hardcoding 0.2.
 *
 * 2. A GRADE WITHOUT ITS FACT. Every signal carries the observation it was derived
 *    from and one sentence of why it matters. A red chip that will not say what it
 *    saw is indistinguishable from a bug (the explainability argument, applied to
 *    trust).
 *
 * Thresholds are declared as data at the top of each rule rather than buried in
 * comparisons, because they are the part someone will want to argue about.
 */

import { sourceName } from './sourceNames';
import type {
  ConfidenceOut,
  LifecycleOut,
  StripCandidate,
  StripCluster,
  StripSource,
} from './types';

export type Grade = 'good' | 'warn' | 'bad' | 'unknown';

export interface TrustSignal {
  /** Stable key, so tests and styling do not depend on the label wording. */
  key: string;
  label: string;
  /** The observation itself, always shown. */
  value: string;
  grade: Grade;
  /** One sentence: why this observation moves trust in that direction. */
  why: string;
}

/** A point in time worth drawing. */
export interface TimelineMark {
  key: string;
  /** Epoch milliseconds. */
  at: number;
  label: string;
  detail: string;
  /** How many records this mark stands for. See timelineMarks. */
  count: number;
  /** Marks sharing this key describe the same kind of fact about the same feed, so
   *  they may be merged when the axis cannot separate them. See mergeNearbyMarks. */
  groupKey: string;
  /** Who the mark is about, kept so a merged mark can rebuild its own label rather
   *  than editing the old one's text. Absent on marks that are about no one feed. */
  subject?: { verb: 'changed' | 'fetched'; agency: string; sourceId: string };
  /**
   * `agency` = the agency's own clock (event time). `system` = ours (when we asked,
   * when the record changed). `timer` = a scheduled consequence, not an observation.
   * The distinction is the two clocks, and it is the whole reason the timeline is
   * banded.
   */
  kind: 'agency' | 'system' | 'timer';
  grade?: Grade;
}

/** A duration worth drawing as a bar. `to === null` means open-ended. */
export interface TimelineSpan {
  key: string;
  label: string;
  from: number;
  to: number | null;
  kind: 'agency' | 'lifecycle';
  detail: string;
}

export interface TimelineDomain {
  min: number;
  max: number;
}

export const MS = 1000;

/** Parse a wire timestamp to epoch ms, or null. Never NaN, which would silently
 * place a mark at the left edge of the chart instead of omitting it. */
export function at(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * Recency at `secondsAhead` from now, given its value now. Pure exponential decay
 * means the future value is the present one scaled - the age it was measured at
 * cancels, so this needs no absolute timestamps and cannot disagree with the server
 * about what "now" was.
 */
export function decayRecency(
  recencyNow: number,
  halfLifeSeconds: number,
  secondsAhead: number,
): number {
  if (halfLifeSeconds <= 0) return recencyNow;
  // NO RUNNING THE CLOCK BACKWARDS. A negative interval would return a recency ABOVE
  // the measured one, and the total would clamp at 1.0 - drawing a past in which the
  // event was fully trusted. It is not merely unknown, it is unknowable from this
  // document: corroboration and completeness also changed as sources arrived, and only
  // recency has a law describing how. So the answer before the measurement is the
  // measurement itself, and callers draw nothing to the left of it.
  if (secondsAhead <= 0) return recencyNow;
  return recencyNow * Math.pow(0.5, secondsAhead / halfLifeSeconds);
}

/**
 * The event's total confidence `secondsAhead` from when it was scored.
 *
 * Only the recency component moves with the clock, so the projection swaps that one
 * term and leaves the rest alone. Doing it any other way - decaying the whole value -
 * would double-count reliability and corroboration as if they aged too, and would
 * read as a much sharper fall than the model actually claims.
 */
export function projectConfidence(
  c: ConfidenceOut,
  recencyWeight: number,
  halfLifeSeconds: number,
  secondsAhead: number,
): number {
  const recencyNow = c.breakdown.recency ?? 0;
  const future = decayRecency(recencyNow, halfLifeSeconds, secondsAhead);
  const value = c.value + recencyWeight * (future - recencyNow);
  return Math.min(1, Math.max(0, value));
}

/**
 * Seconds until confidence falls to `threshold`, or null when it never will.
 *
 * Null is a real answer and not a failure: decay has a FLOOR at
 * `value - weight * recency`, the part of the score that does not age. An event
 * resting on strong corroboration and precise geometry can sit above a 0.5 threshold
 * forever on the strength of those alone, and reporting a bogus crossing time for it
 * would be worse than reporting none.
 */
export function crossingSeconds(
  c: ConfidenceOut,
  recencyWeight: number,
  halfLifeSeconds: number,
  threshold: number,
): number | null {
  const recencyNow = c.breakdown.recency ?? 0;
  const term = recencyWeight * recencyNow;
  if (term <= 0 || halfLifeSeconds <= 0) return null;
  if (c.value <= threshold) return 0; // already there
  const ratio = (threshold - c.value + term) / term;
  if (ratio <= 0) return null; // floor is above the threshold: never crosses
  return halfLifeSeconds * Math.log2(1 / ratio);
}

/** Human duration, coarse on purpose: nobody needs "2.03 days". */
export function fmtDuration(seconds: number): string {
  const s = Math.abs(Math.round(seconds));
  if (s < 90) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  // Hours up to a day, then days. "60m" for an hour and "48h" for a two-day work zone
  // both read as instrument output rather than as durations.
  if (s < 86400) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)}h`;
  if (s < 5184000) return `${Math.round(s / 86400)}d`;
  return `${(s / 31536000).toFixed(1)}y`;
}

/** Signed age relative to now: "4h ago" / "in 2d". */
export function fmtWhen(ms: number, nowMs: number): string {
  const delta = (ms - nowMs) / 1000;
  if (Math.abs(delta) < 45) return 'now';
  return delta < 0 ? `${fmtDuration(delta)} ago` : `in ${fmtDuration(delta)}`;
}

// ---------------------------------------------------------------------------
// Timeline construction
// ---------------------------------------------------------------------------

export interface TimelineInput {
  /** Null for a single source record, which has no lifecycle of its own. */
  lifecycle: LifecycleOut | null;
  /** The records contributing to this event - one for a source-record selection. */
  members: StripCandidate[];
  nowMs: number;
}

/**
 * One place that turns a mark's subject and record count into words, so the initial
 * label and a merged one are written by the same rule rather than the second being
 * patched out of the first.
 */
export function markLabel(mark: TimelineMark, count: number): string {
  const s = mark.subject;
  if (!s) return mark.label;
  if (s.verb === 'fetched') return `we fetched ${sourceName(s.sourceId)}`;
  return count > 1 ? `${s.agency} changed ${count} records` : `${s.agency} changed the record`;
}

/**
 * Every mark the timeline can draw, in time order, omitting the unknowable rather
 * than substituting a plausible value.
 *
 * TWO RULES, both learned from running this against a real snapshot.
 *
 * A missing `sourceUpdatedAt` produces NO mark, not a mark at the fetch time. The
 * feeds that omit it are exactly the ones whose freshness we cannot vouch for, and
 * drawing our own fetch there would state the opposite.
 *
 * MARKS AT THE SAME INSTANT FROM THE SAME FEED COLLAPSE INTO ONE, carrying a count.
 * A nine-member cluster was drawing nine "we fetched az511-events" marks on one pixel
 * column - an unreadable smear that also implied nine separate fetches when there was
 * one. Distinct timestamps stay distinct: two records the agency edited on different
 * days are two facts, and merging those would hide a stale contributor.
 */
export function timelineMarks(input: TimelineInput): TimelineMark[] {
  const { lifecycle, members, nowMs } = input;

  interface Group {
    at: number;
    kind: TimelineMark['kind'];
    agency: string;
    sourceId: string;
    ids: string[];
  }
  const updates = new Map<string, Group>();
  const fetches = new Map<string, Group>();

  const add = (into: Map<string, Group>, when: number, m: StripCandidate) => {
    const key = `${m.sourceId}|${when}`;
    const group = into.get(key);
    if (group) group.ids.push(m.nativeId);
    else
      into.set(key, {
        at: when,
        kind: 'system',
        agency: m.agency,
        sourceId: m.sourceId,
        ids: [m.nativeId],
      });
  };

  for (const m of members) {
    const updated = at(m.sourceUpdatedAt);
    if (updated !== null) add(updates, updated, m);
    const fetched = at(m.retrievedAt);
    if (fetched !== null) add(fetches, fetched, m);
  }

  const marks: TimelineMark[] = [];

  for (const [key, g] of updates) {
    const subject = { verb: 'changed' as const, agency: g.agency, sourceId: g.sourceId };
    marks.push({
      key: `updated-${key}`,
      groupKey: `updated:${g.sourceId}`,
      subject,
      at: g.at,
      label: `${g.agency} changed ${g.ids.length > 1 ? `${g.ids.length} records` : 'the record'}`,
      detail: `${sourceName(g.sourceId)} · ${g.ids.slice(0, 4).join(', ')}${g.ids.length > 4 ? ` +${g.ids.length - 4} more` : ''}`,
      kind: 'system',
      count: g.ids.length,
    });
  }

  for (const [key, g] of fetches) {
    marks.push({
      key: `fetched-${key}`,
      groupKey: `fetched:${g.sourceId}`,
      subject: { verb: 'fetched', agency: g.agency, sourceId: g.sourceId },
      at: g.at,
      label: `we fetched ${sourceName(g.sourceId)}`,
      detail:
        g.ids.length > 1
          ? `one fetch, ${g.ids.length} records on this event`
          : 'one fetch, one record',
      kind: 'system',
      count: g.ids.length,
    });
  }

  if (lifecycle) {
    const expires = at(lifecycle.ttlExpiresAt);
    if (expires !== null) {
      // Where the timer sends it, straight from the published table - no second copy
      // of the state machine in the UI.
      const onTimer = lifecycle.transitions.find((t) => t.triggers.includes('timer_ttl'));
      marks.push({
        key: 'ttl',
        groupKey: 'ttl',
        at: expires,
        label: onTimer ? `TTL expires → ${onTimer.toState}` : 'TTL expires',
        detail: onTimer?.rationale ?? 'timer_ttl',
        kind: 'timer',
        grade: expires < nowMs ? 'bad' : 'warn',
        count: 1,
      });
    }
  }

  // Key as tiebreak so the order is stable across renders rather than dependent on
  // Map iteration for simultaneous marks.
  return marks.sort((a, b) => a.at - b.at || a.key.localeCompare(b.key));
}

/** The bars: the agency's stated window, and time held in the current state. */
export function timelineSpans(
  input: TimelineInput,
  lifecycleState: string | null,
): TimelineSpan[] {
  const { lifecycle, members } = input;
  const spans: TimelineSpan[] = [];

  const starts = members.map((m) => at(m.startTime)).filter((v): v is number => v !== null);
  if (starts.length > 0) {
    // Open-ended when ANY contributor is open-ended: one agency putting an end time on
    // its own record does not close an event another agency is still reporting.
    const openEnded = members.some((m) => m.endTime === null);
    const ends = members.map((m) => at(m.endTime)).filter((v): v is number => v !== null);
    spans.push({
      key: 'agency-window',
      label: 'agency-stated window',
      from: Math.min(...starts),
      to: openEnded || ends.length === 0 ? null : Math.max(...ends),
      kind: 'agency',
      detail: openEnded
        ? 'no end time reported - open-ended, so only the TTL will close it'
        : 'start and end as stated by the reporting agency',
    });
  }

  if (lifecycle) {
    const from = at(lifecycle.enteredAt);
    if (from !== null) {
      spans.push({
        key: 'lifecycle',
        label: lifecycleState ?? 'current state',
        from,
        to: at(lifecycle.ttlExpiresAt),
        kind: 'lifecycle',
        detail: lifecycle.historyAvailable
          ? 'time held in this state'
          : 'first observed on this build - NOT when the event entered this state',
      });
    }
  }

  return spans;
}

/**
 * The drawing window: the marks, plus now, padded by 4% so a mark at an extreme is
 * not drawn on the axis itself.
 *
 * SPANS DELIBERATELY DO NOT SET THE WINDOW. A work zone scheduled to 2027 stretched
 * the axis over thirteen months, which crushed every observation and the TTL deadline
 * into the last tenth of it - the marks an hour apart landed on the same pixel. The
 * span is not lost: it is clipped by `clipSpan` and drawn with an arrow at the edge it
 * runs past, and its exact dates are in the panel beside the chart. Losing the shape
 * of a three-year bar costs nothing; losing the hour around now costs the whole point.
 *
 * `now` is always inside the window, because a timeline that can omit the present
 * moment would let a wholly historical event look current.
 */
export function timelineDomain(
  marks: TimelineMark[],
  nowMs: number,
  /**
   * How far forward the confidence projection wants to be visible. Optional, and NOT a
   * claim that anything happens then - it exists only so the decay curve has room to
   * show its shape. Without it the axis ends at the last mark, which for a work zone
   * left the projection 4% of the plot width and a curve too short to read.
   */
  horizonMs?: number,
): TimelineDomain {
  const points: number[] = [nowMs, ...marks.map((m) => m.at)];
  let min = Math.min(...points);
  let max = Math.max(...points);
  if (max - min < 60 * MS) {
    // Everything inside a minute: give the axis an hour so the marks separate.
    const mid = (min + max) / 2;
    min = mid - 30 * 60 * MS;
    max = mid + 30 * 60 * MS;
  }
  if (horizonMs !== undefined && horizonMs > max) {
    // Bounded: the observed region keeps at least half the axis, so a class with a
    // one-year half-life cannot compress a day of real observations into a sliver for
    // the sake of a curve nobody needs to see the end of.
    max = Math.min(horizonMs, max + Math.max(max - min, 3600 * MS));
  }
  const pad = (max - min) * 0.04;
  return { min: min - pad, max: max + pad };
}

/** How many half-lives of projection to try to show. Three is where the curve has
 *  visibly flattened onto its floor, which is the shape worth reading. */
export const DECAY_HORIZON_HALF_LIVES = 3;

/**
 * Marks the axis cannot separate, merged into one that says how many it stands for.
 *
 * Exact-instant grouping in `timelineMarks` is not enough on a wide axis: two records
 * an agency edited five minutes apart are two distinct facts, but on a 47-day window
 * they are the same pixel, and drawing both produced two identical labels stacked on
 * top of each other. Merging is therefore a function of the DOMAIN, not of the data -
 * which is why it is a separate pass, applied after the window is known.
 *
 * Only marks of the same kind about the same feed ever merge (`groupKey`), and the
 * merged label carries the count with the true range in its detail. A merge never
 * spans sources, so "AZ511 is stale but HERE is current" cannot be flattened away.
 */
export function mergeNearbyMarks(
  marks: TimelineMark[],
  domain: TimelineDomain,
  minSeparation = 0.02,
): TimelineMark[] {
  const window = domain.max - domain.min;
  if (window <= 0) return marks;
  const tolerance = window * minSeparation;

  const merged: TimelineMark[] = [];
  let i = 0;
  while (i < marks.length) {
    const first = marks[i];
    // Runs measured from the run's FIRST mark, not from the latest one: chaining off
    // the latest would let a long drizzle of marks collapse without limit and hide a
    // genuine spread.
    let j = i + 1;
    const group = [first];
    while (
      j < marks.length &&
      marks[j].groupKey === first.groupKey &&
      marks[j].at - first.at <= tolerance
    ) {
      group.push(marks[j]);
      j++;
    }
    if (group.length === 1) {
      merged.push(first);
    } else {
      const count = group.reduce((n, m) => n + m.count, 0);
      const last = group[group.length - 1];
      merged.push({
        ...first,
        // Midpoint: every mark in the group is inside one tolerance band, so no
        // position in it is more true than another. The real range is in the tooltip.
        at: (first.at + last.at) / 2,
        count,
        label: markLabel(first, count),
        detail: `${count} records over ${fmtDuration((last.at - first.at) / 1000)} - closer together than this axis can separate`,
      });
    }
    i = j;
  }
  return merged;
}

export interface ClippedSpan {
  from: number;
  to: number;
  /** True when the real bound lies outside the window, so an arrow must say so. */
  clippedLeft: boolean;
  clippedRight: boolean;
  /** Fully outside the window - draw the edge cap and label, not a bar. */
  offscreen: boolean;
}

/**
 * A span reduced to the drawing window, remembering which ends were cut.
 *
 * An open-ended span (`to === null`) is always clipped right: there is no reported end
 * to draw, and a bar that stopped somewhere would assert one.
 */
export function clipSpan(span: TimelineSpan, domain: TimelineDomain): ClippedSpan {
  const rawTo = span.to === null ? Infinity : span.to;
  const from = Math.max(span.from, domain.min);
  const to = Math.min(rawTo, domain.max);
  return {
    from,
    to: Math.max(from, to),
    clippedLeft: span.from < domain.min,
    clippedRight: rawTo > domain.max,
    // An event whose stated window closed before the window opened, which is a real
    // and important case: the agency says it is over and we are still carrying it.
    offscreen: rawTo < domain.min || span.from > domain.max,
  };
}

// ---------------------------------------------------------------------------
// Trustworthiness signals
// ---------------------------------------------------------------------------

/** Spatial method -> grade. Mirrors the precision table in core/confidence.py; the
 * bands are coarser here because a chip has three states and a score has a hundred. */
const POSITION_GRADE: Record<string, Grade> = {
  native_lrs: 'good',
  milepost: 'good',
  coordinate: 'good',
  sensor_snap: 'warn',
  polygon_intersect: 'warn',
  text_geocode: 'bad',
  unresolved: 'bad',
};

/** How far past a feed's own freshness SLO before silence is a fault rather than a
 * lull. One SLO is the promise; beyond four, the feed is not doing what it says. */
const SLO_WARN_MULTIPLE = 1;
const SLO_BAD_MULTIPLE = 4;

/** The threshold the decay projection reports a crossing time for. Not a system
 * constant - the integrator picks this - so it is the
 * midpoint, labelled as an example wherever it is shown. */
export const EXAMPLE_TRUST_THRESHOLD = 0.5;

const MODE_GRADE: Record<string, Grade> = { live: 'good', fixture: 'warn', failed: 'bad' };

export interface SignalInput {
  members: StripCandidate[];
  sourcesById: Map<string, StripSource>;
  lifecycle: LifecycleOut | null;
  reviewPairCount: number;
  nowMs: number;
}

/**
 * The trust chips, worst first so the reason not to trust an event is never below the
 * fold. Each is one observation; none is a composite of others.
 */
export function trustSignals(input: SignalInput): TrustSignal[] {
  const { members, sourcesById, lifecycle, reviewPairCount, nowMs } = input;
  const signals: TrustSignal[] = [];
  if (members.length === 0) return signals;

  const sources = members
    .map((m) => sourcesById.get(m.sourceId))
    .filter((s): s is StripSource => s !== undefined);

  // --- provenance: are these bytes from the feed, or replayed from a capture? -----
  // First chip on purpose. Everything below is a statement about data whose origin
  // this answers, and a fixture-backed event that looks live is the failure mode the
  // whole app is built to avoid.
  const worstMode =
    sources.find((s) => s.mode === 'failed')?.mode ??
    sources.find((s) => s.mode === 'fixture')?.mode ??
    sources[0]?.mode;
  if (worstMode) {
    signals.push({
      key: 'provenance',
      label: 'provenance',
      value: worstMode === 'live' ? 'live fetch' : worstMode === 'fixture' ? 'captured bytes' : 'adapter failed',
      grade: MODE_GRADE[worstMode] ?? 'unknown',
      why:
        worstMode === 'live'
          ? 'Fetched from the agency this build.'
          : worstMode === 'fixture'
            ? 'Replayed from a stored payload. The content is real but its timestamps are frozen, so freshness below describes the capture, not the corridor.'
            : 'The adapter threw on this payload; what reached the corridor is partial.',
    });
  }

  // --- corroboration: independent sources, not source COUNT ----------------------
  const groups = new Set(sources.map((s) => s.independenceGroup ?? s.sourceId));
  const agencies = new Set(members.map((m) => m.agency));
  signals.push({
    key: 'corroboration',
    label: 'corroboration',
    value:
      groups.size === 1
        ? 'single independent source'
        : `${groups.size} independent sources`,
    grade: groups.size === 1 ? 'warn' : 'good',
    why:
      agencies.size > groups.size
        ? `${agencies.size} agencies report this but only ${groups.size} independent group(s): sources sharing a group resell or mirror the same upstream, so they corroborate once.`
        : groups.size === 1
          ? 'Plausible but uncorroborated - no second, independent source has confirmed it.'
          : 'Confirmed by sources that do not share an upstream.',
  });

  // --- agency freshness: silence measured against the feed's OWN promise ----------
  const confirmedAt = at(lifecycle?.lastConfirmedAt ?? null);
  if (confirmedAt !== null) {
    const ageSeconds = Math.max(0, (nowMs - confirmedAt) / 1000);
    // The SLO of the feed that produced the most recent confirmation. A stale
    // corroborator must not set the standard the fresh one is judged by.
    const freshest = members.reduce<StripCandidate | null>((best, m) => {
      const t = at(m.sourceUpdatedAt) ?? at(m.retrievedAt);
      const bt = best ? (at(best.sourceUpdatedAt) ?? at(best.retrievedAt)) : null;
      return t !== null && (bt === null || t > bt) ? m : best;
    }, null);
    const slo = freshest ? (sourcesById.get(freshest.sourceId)?.freshnessSloSeconds ?? null) : null;
    // The scorer falls back to OUR fetch time when a feed reports no per-record update
    // time (`source_updated_at or retrieved_at`). That keeps recency computable, but it
    // means the age here can be a measure of when we asked rather than when anyone
    // confirmed - so it is graded unknown and labelled, not shown as freshness we do
    // not have. Probe-derived congestion is the live case.
    const confirmedByAgency = freshest?.sourceUpdatedAt != null;
    const grade: Grade = !confirmedByAgency
      ? 'unknown'
      : slo === null
        ? 'unknown'
        : ageSeconds <= slo * SLO_WARN_MULTIPLE
          ? 'good'
          : ageSeconds <= slo * SLO_BAD_MULTIPLE
            ? 'warn'
            : 'bad';
    signals.push({
      key: 'freshness',
      label: confirmedByAgency ? 'last confirmed' : 'last fetched',
      value: `${fmtDuration(ageSeconds)} ago${confirmedByAgency ? '' : ' (our fetch)'}`,
      grade,
      why: !confirmedByAgency
        ? 'This feed reports no per-record update time, so the only timestamp available is our own fetch. This is how long since WE asked, not since anything was confirmed - and it is what recency decays from, so the score is as optimistic as this substitution.'
        : slo === null
          ? 'This feed publishes no freshness SLO, so there is no threshold to judge the silence against.'
          : `The feed's own SLO is ${fmtDuration(slo)}. Confidence decays from this moment, not from when we fetched.`,
    });
  }

  // --- position: how the extent was placed on the corridor -----------------------
  const primary = members[0];
  const accuracy =
    primary.positionalAccuracyMeters !== null
      ? ` ±${Math.round(primary.positionalAccuracyMeters)}m`
      : '';
  signals.push({
    key: 'position',
    label: 'position',
    value: primary.conflationMethod + accuracy,
    grade: POSITION_GRADE[primary.conflationMethod] ?? 'unknown',
    why:
      'How the extent was placed on the corridor. A text-geocoded or polygon-derived' +
      ' position is strong evidence of WHAT and weak evidence of WHERE.',
  });

  // --- time basis: agency-stated schedule vs something observed -------------------
  const openEnded = members.some((m) => m.endTime === null);
  signals.push({
    key: 'time',
    label: 'time basis',
    value: primary.timeConfidence + (openEnded ? ', open-ended' : ''),
    grade: primary.timeConfidence === 'observed' ? 'good' : 'warn',
    why: openEnded
      ? 'No end time reported, so nothing but the TTL will close this event - which is why the timer is drawn above.'
      : 'A scheduled or estimated window is the agency\'s plan, not an observation of the road.',
  });

  // --- what a disappearance would mean (the snapshot ambiguity, per source) -----
  const unconfirmed = (lifecycle?.sourceAbsent ?? []).filter(
    (s) => s.snapshotSemantics !== 'cleared',
  );
  if (lifecycle && lifecycle.sourceAbsent.length > 0) {
    signals.push({
      key: 'snapshot-semantics',
      label: 'if it vanishes',
      value: unconfirmed.length > 0 ? 'meaning unconfirmed' : 'means cleared',
      grade: unconfirmed.length > 0 ? 'warn' : 'good',
      why:
        unconfirmed.length > 0
          ? `${unconfirmed.map((s) => sourceName(s.sourceId)).join(', ')} has not confirmed what a record leaving its snapshot means, so disappearance routes to clearing rather than cleared. Lingering is recoverable; clearing a live hazard in front of a truck is not.`
          : 'The agency has confirmed that a record leaving its snapshot means the event ended.',
    });
  }

  // --- internal consistency ------------------------------------------------------
  const inferred = members.flatMap((m) => m.laneImpacts).filter((l) => l.inferred).length;
  const issues = members.reduce((n, m) => n + m.issueCount, 0);
  if (inferred > 0 || issues > 0 || reviewPairCount > 0) {
    const parts: string[] = [];
    if (issues > 0) parts.push(`${issues} mapping issue(s)`);
    if (inferred > 0) parts.push(`${inferred} inferred lane(s)`);
    if (reviewPairCount > 0) parts.push(`${reviewPairCount} pair(s) in review`);
    signals.push({
      key: 'consistency',
      label: 'consistency',
      value: parts.join(', '),
      grade: 'warn',
      why: 'Values that could not be mapped, lane impacts read out of prose rather than stated, or a match too close to call. Recorded, never defaulted.',
    });
  }

  // --- licence: travels with the event, because screenshots travel ---------------
  const restricted = sources.filter((s) => s.redistributable === false);
  const unknownLicence = sources.filter((s) => s.redistributable === null);
  if (restricted.length > 0 || unknownLicence.length > 0) {
    signals.push({
      key: 'redistribution',
      label: 'redistribution',
      value: restricted.length > 0 ? 'not redistributable' : 'terms unknown',
      grade: restricted.length > 0 ? 'bad' : 'unknown',
      why:
        restricted.length > 0
          ? `${restricted.map((s) => s.licenseShort ?? s.sourceId).join(', ')} forbids republication. This event may inform a decision; it may not be republished.`
          : 'No licence terms recorded for this source. Unknown is not the same as permitted.',
    });
  }

  const order: Record<Grade, number> = { bad: 0, warn: 1, unknown: 2, good: 3 };
  return signals.sort((a, b) => order[a.grade] - order[b.grade]);
}

/** The one-line headline: the worst grade present, since that is what bounds trust. */
export function worstGrade(signals: TrustSignal[]): Grade {
  for (const g of ['bad', 'warn', 'unknown', 'good'] as Grade[]) {
    if (signals.some((s) => s.grade === g)) return g;
  }
  return 'good';
}

/** Members of a cluster, in the document's order, skipping any that have aged out of
 * the current snapshot. */
export function clusterMembers(
  cluster: StripCluster,
  candidates: Map<number, StripCandidate>,
): StripCandidate[] {
  return cluster.members
    .map((i) => candidates.get(i))
    .filter((c): c is StripCandidate => c !== undefined);
}
