/**
 * Trust and timeline tests.
 *
 * The properties worth pinning here are the ones where a plausible wrong answer is
 * indistinguishable from a right one on screen:
 *
 *  - A PROJECTION THAT OVERSTATES DECAY. Decaying the whole confidence value instead
 *    of its recency term produces a curve that looks reasonable and is wrong by the
 *    weight of every other component. Nobody would spot it in a screenshot.
 *  - A CROSSING TIME THAT DOES NOT EXIST. Decay has a floor, so an event can sit above
 *    a threshold forever. Reporting "crosses in 3d" for one that never will is worse
 *    than reporting nothing, because it invites a consumer to schedule around it.
 *  - A MARK STANDING IN FOR A MISSING TIMESTAMP. Feeds that omit `sourceUpdatedAt` are
 *    exactly the ones whose freshness we cannot vouch for; drawing our own fetch time
 *    there would state the opposite of the truth.
 *  - COUNTING SOURCES AS CORROBORATION. Two feeds reselling one upstream are one piece
 *    of evidence, and the chip must say so.
 */

import { describe, expect, it } from 'vitest';
import {
  EXAMPLE_TRUST_THRESHOLD,
  clipSpan,
  crossingSeconds,
  decayRecency,
  fmtDuration,
  fmtWhen,
  mergeNearbyMarks,
  projectConfidence,
  timelineDomain,
  timelineMarks,
  timelineSpans,
  trustSignals,
  worstGrade,
  type TimelineSpan,
} from './trust';
import type { ConfidenceOut, LifecycleOut, StripCandidate, StripSource } from './types';

const HOUR = 3600;
const NOW = Date.parse('2026-08-12T12:00:00.000Z');

function confidence(value: number, recency: number): ConfidenceOut {
  return {
    value,
    breakdown: {
      sourceReliability: 0.8,
      corroboration: 0.5,
      recency,
      spatialPrecision: 0.9,
      completeness: 1,
      internalConsistency: 1,
    },
    explanation: [],
  };
}

function candidate(over: Partial<StripCandidate> = {}): StripCandidate {
  return {
    id: 0,
    sourceId: 'a-dot-wzdx',
    agency: 'A DOT',
    nativeId: 'wz-1',
    eventClass: 'work_zone',
    eventSubtype: 'lane_closure',
    beginMeasure: 100,
    endMeasure: 104,
    direction: 'EB',
    states: ['AZ'],
    beginLabel: 'AZ MP 100.0',
    endLabel: 'AZ MP 104.0',
    conflationMethod: 'milepost',
    positionalAccuracyMeters: 50,
    startTime: '2026-08-10T06:00:00.000Z',
    endTime: '2026-08-20T06:00:00.000Z',
    timeConfidence: 'scheduled',
    sourceUpdatedAt: '2026-08-12T11:00:00.000Z',
    retrievedAt: '2026-08-12T11:59:00.000Z',
    laneImpacts: [],
    agencySeverity: 'minor',
    confidence: confidence(0.7, 0.5),
    rawRef: 's3://amzn-s3-demo-rawzone/a/1',
    issueCount: 0,
    ...over,
  };
}

function source(over: Partial<StripSource> = {}): StripSource {
  return {
    sourceId: 'a-dot-wzdx',
    agency: 'A DOT',
    label: 'A DOT WZDx',
    mode: 'live',
    httpStatus: 200,
    latencyMs: 120,
    payloadBytes: 4096,
    candidateCount: 1,
    offCorridor: 0,
    issues: [],
    note: null,
    redistributable: true,
    licenseShort: 'public domain',
    attribution: null,
    independenceGroup: 'adot',
    snapshotSemantics: 'cleared',
    publishCadenceSeconds: 60,
    freshnessSloSeconds: 900,
    ...over,
  };
}

function lifecycle(over: Partial<LifecycleOut> = {}): LifecycleOut {
  return {
    enteredAt: '2026-08-12T11:59:00.000Z',
    ttlExpiresAt: '2026-08-12T12:59:00.000Z',
    lastConfirmedAt: '2026-08-12T11:00:00.000Z',
    reopenWindowSeconds: 604800,
    confidenceHalfLifeSeconds: 604800,
    transitions: [
      {
        toState: 'validated',
        triggers: ['validation_pass'],
        rationale: 'checks passed',
      },
      {
        toState: 'cleared',
        triggers: ['validation_fail', 'timer_ttl'],
        rationale: 'never corroborated',
      },
    ],
    sourceAbsent: [
      {
        sourceId: 'a-dot-wzdx',
        snapshotSemantics: 'cleared',
        toState: 'cleared',
        reason: 'source_snapshot_confirmed_cleared',
      },
    ],
    historyAvailable: false,
    note: 'single snapshot',
    ...over,
  };
}

const SOURCES = new Map([['a-dot-wzdx', source()]]);

describe('recency decay', () => {
  it('halves over one half-life', () => {
    expect(decayRecency(0.8, HOUR, HOUR)).toBeCloseTo(0.4);
    expect(decayRecency(0.8, HOUR, 2 * HOUR)).toBeCloseTo(0.2);
  });

  it('is unchanged at zero elapsed time', () => {
    expect(decayRecency(0.8, HOUR, 0)).toBeCloseTo(0.8);
  });

  it('does not divide by a zero half-life', () => {
    expect(decayRecency(0.8, 0, HOUR)).toBe(0.8);
  });

  it('refuses to run the clock backwards', () => {
    // Extrapolating back inflated recency above the measured value, the total clamped
    // at 1.0, and the chart drew a flat line across weeks of "perfectly trusted" past
    // that never existed. Confidence before the measurement is unknowable here:
    // corroboration and completeness moved as sources arrived, and only recency has a
    // law describing how it changed.
    expect(decayRecency(0.8, HOUR, -HOUR)).toBe(0.8);
    expect(projectConfidence(confidence(0.7, 0.5), 0.2, HOUR, -10 * HOUR)).toBeCloseTo(0.7);
  });
});

describe('confidence projection', () => {
  const c = confidence(0.7, 0.5);
  const W = 0.2; // the published recency weight

  it('moves only the recency term, not the whole value', () => {
    // After one half-life recency goes 0.5 -> 0.25, so the total falls by
    // 0.2 * 0.25 = 0.05. Decaying the VALUE instead would give 0.35 - the defect
    // this test exists for.
    expect(projectConfidence(c, W, HOUR, HOUR)).toBeCloseTo(0.65);
    expect(projectConfidence(c, W, HOUR, 0)).toBeCloseTo(0.7);
  });

  it('settles on the floor rather than falling to zero', () => {
    // Everything the score does not owe to recency survives any amount of silence.
    const floor = 0.7 - W * 0.5;
    expect(projectConfidence(c, W, HOUR, 1000 * HOUR)).toBeCloseTo(floor, 3);
  });

  it('never leaves 0..1', () => {
    for (const ahead of [-HOUR, 0, HOUR, 1e9]) {
      const v = projectConfidence(c, W, HOUR, ahead);
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThanOrEqual(1);
    }
  });
});

describe('threshold crossing', () => {
  const W = 0.2;

  it('finds the moment the curve meets the threshold', () => {
    // value 0.7, recency 0.5, weight 0.2 -> floor 0.6. A 0.65 threshold is halfway
    // down the decaying part, which is exactly one half-life.
    const seconds = crossingSeconds(confidence(0.7, 0.5), W, HOUR, 0.65);
    expect(seconds).not.toBeNull();
    expect(seconds!).toBeCloseTo(HOUR, 0);
  });

  it('returns null when the floor sits above the threshold', () => {
    // Floor 0.6 never reaches 0.5, however long the silence runs. A number here
    // would invite a consumer to schedule around an expiry that never happens.
    expect(crossingSeconds(confidence(0.7, 0.5), W, HOUR, EXAMPLE_TRUST_THRESHOLD)).toBeNull();
  });

  it('reports zero for an event already below the threshold', () => {
    expect(crossingSeconds(confidence(0.4, 0.5), W, HOUR, 0.5)).toBe(0);
  });

  it('returns null when recency contributes nothing to decay', () => {
    expect(crossingSeconds(confidence(0.7, 0), W, HOUR, 0.5)).toBeNull();
  });
});

describe('timeline marks', () => {
  it('draws both clocks: the agency edit and our fetch', () => {
    const marks = timelineMarks({ lifecycle: null, members: [candidate()], nowMs: NOW });
    expect(marks.map((m) => m.label)).toEqual([
      'A DOT changed the record',
      'we fetched a-dot-wzdx',
    ]);
    expect(marks[0].at).toBeLessThan(marks[1].at);
  });

  it('omits the agency mark entirely when the feed does not report one', () => {
    // NOT a mark at the fetch time: that would claim the agency confirmed the record
    // when we asked, which is the opposite of what an absent field means.
    const marks = timelineMarks({
      lifecycle: null,
      members: [candidate({ sourceUpdatedAt: null })],
      nowMs: NOW,
    });
    expect(marks.map((m) => m.label)).toEqual(['we fetched a-dot-wzdx']);
  });

  it('collapses one fetch of many records into one mark', () => {
    // A nine-member cluster drew nine identical fetch marks on one pixel column -
    // unreadable, and it implied nine fetches where there was one.
    const marks = timelineMarks({
      lifecycle: null,
      members: [
        candidate({ id: 0, nativeId: 'wz-1' }),
        candidate({ id: 1, nativeId: 'wz-2' }),
        candidate({ id: 2, nativeId: 'wz-3' }),
      ],
      nowMs: NOW,
    });
    const fetched = marks.filter((m) => m.label.startsWith('we fetched'));
    expect(fetched).toHaveLength(1);
    expect(fetched[0].count).toBe(3);
    expect(fetched[0].detail).toContain('one fetch, 3 records');
  });

  it('keeps records the agency edited at different times as separate marks', () => {
    // These are two facts, and merging them would hide a stale contributor inside an
    // otherwise fresh event.
    const marks = timelineMarks({
      lifecycle: null,
      members: [
        candidate({ id: 0, sourceUpdatedAt: '2026-08-12T11:00:00.000Z' }),
        candidate({ id: 1, sourceUpdatedAt: '2026-07-01T11:00:00.000Z' }),
      ],
      nowMs: NOW,
    });
    expect(marks.filter((m) => m.label.includes('changed'))).toHaveLength(2);
  });

  it('counts records sharing one agency edit time', () => {
    const marks = timelineMarks({
      lifecycle: null,
      members: [candidate({ id: 0 }), candidate({ id: 1, nativeId: 'wz-2' })],
      nowMs: NOW,
    });
    const changed = marks.find((m) => m.label.includes('changed'))!;
    expect(changed.label).toBe('A DOT changed 2 records');
  });

  it('keeps feeds apart even when they were fetched in the same pass', () => {
    const marks = timelineMarks({
      lifecycle: null,
      members: [candidate(), candidate({ id: 1, sourceId: 'b-dot', agency: 'B DOT' })],
      nowMs: NOW,
    });
    expect(marks.filter((m) => m.label.startsWith('we fetched'))).toHaveLength(2);
  });

  it('labels the TTL mark with where the timer actually routes', () => {
    const marks = timelineMarks({
      lifecycle: lifecycle(),
      members: [candidate()],
      nowMs: NOW,
    });
    const ttl = marks.find((m) => m.key === 'ttl');
    // Read from the served transition table, not from a copy in the UI.
    expect(ttl?.label).toBe('TTL expires → cleared');
    expect(ttl?.grade).toBe('warn');
  });

  it('flags a TTL that has already expired', () => {
    const marks = timelineMarks({
      lifecycle: lifecycle({ ttlExpiresAt: '2026-08-12T10:00:00.000Z' }),
      members: [candidate()],
      nowMs: NOW,
    });
    expect(marks.find((m) => m.key === 'ttl')?.grade).toBe('bad');
  });

  it('skips the TTL mark when the state has no timer', () => {
    const marks = timelineMarks({
      lifecycle: lifecycle({ ttlExpiresAt: null }),
      members: [candidate()],
      nowMs: NOW,
    });
    expect(marks.some((m) => m.key === 'ttl')).toBe(false);
  });
});

describe('timeline spans', () => {
  it('treats the window as open-ended when any contributor is', () => {
    // One agency putting an end time on its own record does not close an event
    // another agency is still reporting with none.
    const spans = timelineSpans(
      {
        lifecycle: null,
        members: [candidate(), candidate({ id: 1, endTime: null })],
        nowMs: NOW,
      },
      null,
    );
    const window = spans.find((s) => s.key === 'agency-window');
    expect(window?.to).toBeNull();
  });

  it('spans the widest stated window across contributors', () => {
    const spans = timelineSpans(
      {
        lifecycle: null,
        members: [
          candidate(),
          candidate({
            id: 1,
            startTime: '2026-08-09T06:00:00.000Z',
            endTime: '2026-08-25T06:00:00.000Z',
          }),
        ],
        nowMs: NOW,
      },
      null,
    );
    const window = spans.find((s) => s.key === 'agency-window')!;
    expect(window.from).toBe(Date.parse('2026-08-09T06:00:00.000Z'));
    expect(window.to).toBe(Date.parse('2026-08-25T06:00:00.000Z'));
  });

  it('does not present enteredAt as the start of the state when there is no history', () => {
    const spans = timelineSpans(
      { lifecycle: lifecycle(), members: [candidate()], nowMs: NOW },
      'reported',
    );
    const life = spans.find((s) => s.key === 'lifecycle')!;
    expect(life.detail).toContain('NOT when the event entered this state');
  });
});

describe('timeline domain', () => {
  it('always contains now, even for a wholly historical event', () => {
    // A timeline that could omit the present moment would let an event that ended
    // last week look current.
    const members = [
      candidate({
        startTime: '2026-01-01T00:00:00.000Z',
        endTime: '2026-01-02T00:00:00.000Z',
        sourceUpdatedAt: '2026-01-01T00:00:00.000Z',
        retrievedAt: '2026-01-01T00:00:00.000Z',
      }),
    ];
    const domain = timelineDomain(timelineMarks({ lifecycle: null, members, nowMs: NOW }), NOW);
    expect(domain.min).toBeLessThan(Date.parse('2026-01-01T00:00:00.000Z'));
    expect(domain.max).toBeGreaterThan(NOW);
  });

  it('widens a window where everything happened at once', () => {
    const t = '2026-08-12T12:00:00.000Z';
    const members = [candidate({ sourceUpdatedAt: t, retrievedAt: t })];
    const domain = timelineDomain(timelineMarks({ lifecycle: null, members, nowMs: NOW }), NOW);
    expect(domain.max - domain.min).toBeGreaterThan(1800 * 1000);
  });

  it('makes forward room for the decay projection when asked', () => {
    // The axis used to end at the last mark, which left the curve 4% of the plot width.
    const marks = timelineMarks({ lifecycle: lifecycle(), members: [candidate()], nowMs: NOW });
    const bare = timelineDomain(marks, NOW);
    const withHorizon = timelineDomain(marks, NOW, NOW + 6 * 3600_000);
    expect(withHorizon.max).toBeGreaterThan(bare.max);
  });

  it('caps the forward room so observations keep at least half the axis', () => {
    // A dimensional restriction has a one-year half-life. Granting three of them would
    // squeeze a day of real observations into a sliver for a curve whose tail nobody
    // needs to see.
    const marks = timelineMarks({ lifecycle: lifecycle(), members: [candidate()], nowMs: NOW });
    const bare = timelineDomain(marks, NOW);
    const observed = bare.max - bare.min;
    const greedy = timelineDomain(marks, NOW, NOW + 3 * 31536000_000);
    expect(greedy.max - greedy.min).toBeLessThanOrEqual(observed * 2 + 3600_000);
  });

  it('ignores a horizon that is already inside the window', () => {
    const marks = timelineMarks({ lifecycle: lifecycle(), members: [candidate()], nowMs: NOW });
    const bare = timelineDomain(marks, NOW);
    expect(timelineDomain(marks, NOW, NOW - 86400_000).max).toBe(bare.max);
  });

  it('is not stretched by a multi-year stated window', () => {
    // A work zone scheduled into 2027 stretched the axis over thirteen months and
    // crushed the observations and the TTL deadline into its last tenth - marks an
    // hour apart landed on the same pixel. The span is clipped instead.
    const members = [
      candidate({
        startTime: '2026-03-02T12:15:00.000Z',
        endTime: '2027-03-26T13:00:00.000Z',
        sourceUpdatedAt: '2026-08-12T11:00:00.000Z',
        retrievedAt: '2026-08-12T11:59:00.000Z',
      }),
    ];
    const input = { lifecycle: lifecycle(), members, nowMs: NOW };
    const domain = timelineDomain(timelineMarks(input), NOW);
    expect(domain.max).toBeLessThan(Date.parse('2026-08-13T00:00:00.000Z'));
    // ...and the span still reports that it runs past both edges.
    const window = timelineSpans(input, 'reported').find((s) => s.key === 'agency-window')!;
    const clip = clipSpan(window, domain);
    expect(clip.clippedLeft).toBe(true);
    expect(clip.clippedRight).toBe(true);
    expect(clip.offscreen).toBe(false);
  });
});

describe('merging marks the axis cannot separate', () => {
  /** Two records the agency edited five minutes apart - distinct facts that land on
   *  the same pixel of a 47-day window, which is how they got stacked on screen. */
  const members = [
    candidate({ id: 0, sourceUpdatedAt: '2026-06-29T10:00:00.000Z' }),
    candidate({ id: 1, nativeId: 'wz-2', sourceUpdatedAt: '2026-06-29T10:05:00.000Z' }),
  ];
  const input = { lifecycle: lifecycle(), members, nowMs: NOW };

  it('merges them on a wide axis and says how many it stands for', () => {
    const raw = timelineMarks(input);
    expect(raw.filter((m) => m.groupKey.startsWith('updated'))).toHaveLength(2);

    const wide = { min: NOW - 47 * 86400_000, max: NOW + 86400_000 };
    const merged = mergeNearbyMarks(raw, wide).filter((m) =>
      m.groupKey.startsWith('updated'),
    );
    expect(merged).toHaveLength(1);
    expect(merged[0].count).toBe(2);
    expect(merged[0].label).toBe('A DOT changed 2 records');
    expect(merged[0].detail).toContain('closer together than this axis can separate');
  });

  it('leaves them apart on an axis narrow enough to show both', () => {
    // A two-hour window puts the tolerance at ~2.4 minutes, so five minutes apart is
    // separable and must stay two marks.
    const narrow = {
      min: Date.parse('2026-06-29T09:00:00.000Z'),
      max: Date.parse('2026-06-29T11:00:00.000Z'),
    };
    expect(
      mergeNearbyMarks(timelineMarks(input), narrow).filter((m) =>
        m.groupKey.startsWith('updated'),
      ),
    ).toHaveLength(2);
  });

  it('never merges across feeds', () => {
    // Flattening these would hide "AZ511 is stale but HERE is current" - the exact
    // thing a per-source mark exists to show.
    const twoFeeds = timelineMarks({
      lifecycle: null,
      members: [
        candidate({ id: 0, sourceUpdatedAt: '2026-06-29T10:00:00.000Z' }),
        candidate({
          id: 1,
          sourceId: 'b-dot',
          agency: 'B DOT',
          sourceUpdatedAt: '2026-06-29T10:05:00.000Z',
        }),
      ],
      nowMs: NOW,
    });
    const wide = { min: NOW - 47 * 86400_000, max: NOW + 86400_000 };
    expect(mergeNearbyMarks(twoFeeds, wide)).toHaveLength(twoFeeds.length);
  });

  it('never merges an observation into the TTL deadline', () => {
    const marks = timelineMarks(input);
    const wide = { min: NOW - 47 * 86400_000, max: NOW + 86400_000 };
    const merged = mergeNearbyMarks(marks, wide);
    expect(merged.filter((m) => m.groupKey === 'ttl')).toHaveLength(1);
  });

  it('measures each run from its first mark, not from a moving latest', () => {
    // A drizzle of marks each just inside tolerance of the previous one must not chain
    // into one blob spanning the whole axis.
    const drizzle = Array.from({ length: 6 }, (_, i) =>
      candidate({
        id: i,
        nativeId: `wz-${i}`,
        sourceUpdatedAt: new Date(NOW - (6 - i) * 3600_000).toISOString(),
      }),
    );
    const raw = timelineMarks({ lifecycle: null, members: drizzle, nowMs: NOW });
    // Tolerance ~1.5h over a 3-day window: pairs collapse, the whole run must not.
    const merged = mergeNearbyMarks(raw, { min: NOW - 3 * 86400_000, max: NOW }, 0.02);
    const updates = merged.filter((m) => m.groupKey.startsWith('updated'));
    expect(updates.length).toBeGreaterThan(1);
    expect(updates.reduce((n, m) => n + m.count, 0)).toBe(6);
  });

  it('preserves every record it collapses', () => {
    const raw = timelineMarks(input);
    const wide = { min: NOW - 47 * 86400_000, max: NOW + 86400_000 };
    const before = raw.reduce((n, m) => n + m.count, 0);
    expect(mergeNearbyMarks(raw, wide).reduce((n, m) => n + m.count, 0)).toBe(before);
  });
});

describe('span clipping', () => {
  const domain = { min: NOW - 3600_000, max: NOW + 3600_000 };

  function span(from: string, to: string | null): TimelineSpan {
    return {
      key: 'agency-window',
      label: 'agency-stated window',
      from: Date.parse(from),
      to: to === null ? null : Date.parse(to),
      kind: 'agency',
      detail: '',
    };
  }

  it('leaves a span that fits alone', () => {
    const clip = clipSpan(span('2026-08-12T11:30:00.000Z', '2026-08-12T12:30:00.000Z'), domain);
    expect(clip.clippedLeft).toBe(false);
    expect(clip.clippedRight).toBe(false);
    expect(clip.offscreen).toBe(false);
  });

  it('always clips an open-ended span on the right', () => {
    // There is no reported end, so a bar that stopped somewhere would assert one.
    const clip = clipSpan(span('2026-08-12T11:30:00.000Z', null), domain);
    expect(clip.clippedRight).toBe(true);
    expect(clip.to).toBe(domain.max);
  });

  it('reports a window that closed before the view opened as offscreen', () => {
    // "The agency says this is over and we are still carrying it" is too important to
    // become an invisible zero-width bar at the left edge.
    const clip = clipSpan(span('2026-08-01T00:00:00.000Z', '2026-08-02T00:00:00.000Z'), domain);
    expect(clip.offscreen).toBe(true);
  });

  it('reports a window that opens after the view ends as offscreen', () => {
    const clip = clipSpan(span('2026-09-01T00:00:00.000Z', '2026-09-02T00:00:00.000Z'), domain);
    expect(clip.offscreen).toBe(true);
  });

  it('never produces a negative width', () => {
    const clip = clipSpan(span('2026-08-12T13:30:00.000Z', '2026-08-12T14:00:00.000Z'), domain);
    expect(clip.to).toBeGreaterThanOrEqual(clip.from);
  });
});

describe('trust signals', () => {
  const base = {
    members: [candidate()],
    sourcesById: SOURCES,
    lifecycle: lifecycle(),
    reviewPairCount: 0,
    nowMs: NOW,
  };

  function signal(signals: ReturnType<typeof trustSignals>, key: string) {
    const found = signals.find((s) => s.key === key);
    expect(found, `expected a '${key}' signal`).toBeDefined();
    return found!;
  }

  it('says when the bytes were replayed rather than fetched', () => {
    const signals = trustSignals({
      ...base,
      sourcesById: new Map([['a-dot-wzdx', source({ mode: 'fixture' })]]),
    });
    expect(signal(signals, 'provenance').grade).toBe('warn');
    expect(signal(signals, 'provenance').value).toBe('captured bytes');
  });

  it('counts independence groups, not agencies', () => {
    // Two agencies reselling one upstream is ONE piece of evidence. Counting
    // sources here would inflate confidence exactly where it should not.
    const signals = trustSignals({
      ...base,
      members: [candidate(), candidate({ id: 1, sourceId: 'b-dot', agency: 'B DOT' })],
      sourcesById: new Map([
        ['a-dot-wzdx', source()],
        ['b-dot', source({ sourceId: 'b-dot', independenceGroup: 'adot' })],
      ]),
    });
    const c = signal(signals, 'corroboration');
    expect(c.value).toBe('single independent source');
    expect(c.grade).toBe('warn');
    expect(c.why).toContain('2 agencies');
  });

  it('credits genuinely independent sources', () => {
    const signals = trustSignals({
      ...base,
      members: [candidate(), candidate({ id: 1, sourceId: 'b-dot', agency: 'B DOT' })],
      sourcesById: new Map([
        ['a-dot-wzdx', source()],
        ['b-dot', source({ sourceId: 'b-dot', independenceGroup: 'bdot' })],
      ]),
    });
    expect(signal(signals, 'corroboration').grade).toBe('good');
  });

  it('judges staleness against the feed’s own SLO', () => {
    // An hour of silence is fine for a daily feed and a fault for a 60-second one, so
    // the threshold cannot be global.
    const fresh = trustSignals({ ...base, lifecycle: lifecycle({ lastConfirmedAt: '2026-08-12T11:55:00.000Z' }) });
    expect(signal(fresh, 'freshness').grade).toBe('good');

    const stale = trustSignals({ ...base, lifecycle: lifecycle({ lastConfirmedAt: '2026-07-22T11:00:00.000Z' }) });
    expect(signal(stale, 'freshness').grade).toBe('bad');
  });

  it('does not present our own fetch time as a confirmation', () => {
    // The scorer substitutes `retrievedAt` when a feed reports no update time, which
    // keeps recency computable but makes it a measure of when WE asked. Showing that as
    // "last confirmed 2m ago" would launder a fetch into corroboration.
    const signals = trustSignals({
      ...base,
      members: [candidate({ sourceUpdatedAt: null })],
      lifecycle: lifecycle({ lastConfirmedAt: '2026-08-12T11:59:00.000Z' }),
    });
    const s = signal(signals, 'freshness');
    expect(s.label).toBe('last fetched');
    expect(s.value).toContain('our fetch');
    expect(s.grade).toBe('unknown');
  });

  it('grades freshness unknown when the feed publishes no SLO', () => {
    const signals = trustSignals({
      ...base,
      sourcesById: new Map([['a-dot-wzdx', source({ freshnessSloSeconds: null })]]),
    });
    expect(signal(signals, 'freshness').grade).toBe('unknown');
  });

  it('marks a text-geocoded position as weak evidence of where', () => {
    const signals = trustSignals({
      ...base,
      members: [candidate({ conflationMethod: 'text_geocode' })],
    });
    expect(signal(signals, 'position').grade).toBe('bad');
  });

  it('warns that a disappearance would not mean cleared', () => {
    const signals = trustSignals({
      ...base,
      lifecycle: lifecycle({
        sourceAbsent: [
          {
            sourceId: 'a-dot-wzdx',
            snapshotSemantics: 'UNKNOWN',
            toState: 'clearing',
            reason: 'stale_no_updates_semantics_unconfirmed',
          },
        ],
      }),
    });
    const s = signal(signals, 'snapshot-semantics');
    expect(s.grade).toBe('warn');
    expect(s.why).toContain('clearing');
  });

  it('carries a redistribution ban onto the event itself', () => {
    // The strip is the artifact someone screenshots into a deck.
    const signals = trustSignals({
      ...base,
      sourcesById: new Map([
        ['a-dot-wzdx', source({ redistributable: false, licenseShort: 'HERE terms' })],
      ]),
    });
    expect(signal(signals, 'redistribution').grade).toBe('bad');
  });

  it('treats unknown licence terms as unknown, not as permission', () => {
    const signals = trustSignals({
      ...base,
      sourcesById: new Map([['a-dot-wzdx', source({ redistributable: null })]]),
    });
    expect(signal(signals, 'redistribution').grade).toBe('unknown');
  });

  it('reports inferred lanes, mapping issues, and pairs left in review', () => {
    const signals = trustSignals({
      ...base,
      members: [
        candidate({
          issueCount: 3,
          laneImpacts: [{ ordinal: 1, type: 'general', status: 'closed', inferred: true }],
        }),
      ],
      reviewPairCount: 2,
    });
    const s = signal(signals, 'consistency');
    expect(s.value).toContain('3 mapping issue');
    expect(s.value).toContain('1 inferred lane');
    expect(s.value).toContain('2 pair');
  });

  it('omits the consistency chip when there is nothing to report', () => {
    expect(trustSignals(base).some((s) => s.key === 'consistency')).toBe(false);
  });

  it('every signal states the observation and the reason it matters', () => {
    for (const s of trustSignals(base)) {
      expect(s.value, `${s.key} has no value`).toBeTruthy();
      expect(s.why.length, `${s.key} has no reasoning`).toBeGreaterThan(20);
    }
  });

  it('orders the worst signals first', () => {
    const signals = trustSignals({
      ...base,
      sourcesById: new Map([['a-dot-wzdx', source({ mode: 'failed' })]]),
    });
    expect(signals[0].grade).toBe('bad');
    expect(worstGrade(signals)).toBe('bad');
  });

  it('returns nothing rather than guessing for an event with no records', () => {
    expect(trustSignals({ ...base, members: [] })).toEqual([]);
  });
});

describe('formatting', () => {
  it('stays coarse across the full range of feed cadences', () => {
    expect(fmtDuration(45)).toBe('45s');
    expect(fmtDuration(900)).toBe('15m');
    expect(fmtDuration(3600)).toBe('1.0h');
    expect(fmtDuration(7200)).toBe('2.0h');
    expect(fmtDuration(86400)).toBe('1d');
    expect(fmtDuration(604800)).toBe('7d');
    expect(fmtDuration(31536000)).toBe('1.0y');
  });

  it('distinguishes past from future', () => {
    expect(fmtWhen(NOW - 3600_000, NOW)).toBe('1.0h ago');
    expect(fmtWhen(NOW + 86400_000, NOW)).toBe('in 1d');
    expect(fmtWhen(NOW, NOW)).toBe('now');
  });
});
