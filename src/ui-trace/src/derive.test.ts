/**
 * Tests for the tracker's arithmetic.
 *
 * The cases chosen are the ones where being wrong is INVISIBLE in the rendered
 * output: a collapsed run that swallows a transition, a band chart that renders a
 * short state as zero pixels, a diff that reports no changes on a version that
 * exists. Every one of those looks like a normal screen and says something false.
 */

import { describe, expect, it } from 'vitest';
import {
  extentLabel,
  filterRecords,
  groupSteps,
  humanBytes,
  humanDuration,
  isSuspect,
  quietLevel,
  refLabel,
  sortChanges,
  sortFindings,
  stateBands,
  ttlProgress,
  visibleChanges,
  worstSeverity,
} from './derive';
import type { Change, Finding, RecordRow, Step, StateSpan } from './types';

function step(overrides: Partial<Step> = {}): Step {
  return {
    sequence: 1,
    stage: 'ingest',
    from_state: 'active',
    to_state: 'active',
    trigger: 'source_update',
    actor: 'adapter',
    reason: 'feed updated its record',
    rule_version: '0.1.0',
    occurred_at: '2026-08-13T12:00:00.000Z',
    recorded_at: '2026-08-13T12:00:00.000Z',
    lag_seconds: 0,
    operator_id: null,
    payload_ref: 's3://amzn-s3-demo-rawzone/one.json',
    transition: false,
    legal: true,
    version: 1,
    confidence: 0.7,
    version_missing: false,
    changes: [],
    confirmation_only: true,
    diff_unavailable: false,
    ...overrides,
  };
}

function row(overrides: Partial<RecordRow> = {}): RecordRow {
  return {
    event_id: '01KZVRGV3A4WVSVAH9CNYFKFAK',
    event_class: 'work_zone',
    event_subtype: 'work-zone',
    lifecycle_state: 'active',
    version: 3,
    confidence: 0.61,
    severity: 'moderate',
    direction: 'EB',
    states: ['OK'],
    begin_measure: 1100.2,
    end_measure: 1100.9,
    milepost_begin: { state: 'OK', milepost: 12.3 },
    milepost_end: { state: 'OK', milepost: 13.0 },
    conflation_method: 'coordinate',
    agencies: ['Oklahoma DOT'],
    source_ids: ['ok-odot-wzdx'],
    native_ids: ['250182-1'],
    independent_source_count: 1,
    created_at: '2026-08-12T19:52:23.479Z',
    updated_at: '2026-08-13T21:53:23.160Z',
    start_time: '2026-08-12T19:00:00.000Z',
    end_time: null,
    last_source_update_at: '2026-08-13T21:52:00.000Z',
    ttl_expires_at: '2026-08-20T21:53:23.160Z',
    ttl_expired: false,
    seconds_until_ttl: 600_000,
    age_seconds: 93_000,
    quiet_seconds: 20,
    related_event_ids: [],
    unresolved_extent: false,
    terminal: false,
    ...overrides,
  };
}

describe('humanDuration', () => {
  it('changes unit where the previous one stops being legible', () => {
    expect(humanDuration(0.4)).toBe('<1s');
    expect(humanDuration(45)).toBe('45s');
    expect(humanDuration(600)).toBe('10m');
    expect(humanDuration(7200)).toBe('2.0h');
    expect(humanDuration(259200)).toBe('3.0d');
  });

  it('renders an absent duration as unknown rather than as zero', () => {
    // "0s" would claim the record spent no time in a state. It is a different fact.
    expect(humanDuration(null)).toBe('-');
    expect(humanDuration(undefined)).toBe('-');
    expect(humanDuration(NaN)).toBe('-');
  });
});

describe('groupSteps', () => {
  it('collapses a run of confirmations into one row with a count', () => {
    const steps = [
      step({ sequence: 1, confirmation_only: false, transition: true, from_state: null, to_state: 'reported' }),
      step({ sequence: 2, recorded_at: '2026-08-13T12:01:00.000Z' }),
      step({ sequence: 3, recorded_at: '2026-08-13T12:02:00.000Z' }),
      step({ sequence: 4, recorded_at: '2026-08-13T12:03:00.000Z' }),
    ];
    const groups = groupSteps(steps);
    expect(groups.map((g) => g.kind)).toEqual(['step', 'run']);
    const run = groups[1];
    if (run.kind !== 'run') throw new Error('expected a run');
    expect(run.count).toBe(3);
    expect(run.seconds).toBe(120);
  });

  it('never collapses across a transition', () => {
    // The failure this guards: a run that swallows the one step that moved the
    // record, leaving a screen that says nothing happened.
    const steps = [
      step({ sequence: 1 }),
      step({ sequence: 2 }),
      step({
        sequence: 3,
        confirmation_only: false,
        transition: true,
        from_state: 'active',
        to_state: 'clearing',
      }),
      step({ sequence: 4, from_state: 'clearing', to_state: 'clearing' }),
      step({ sequence: 5, from_state: 'clearing', to_state: 'clearing' }),
    ];
    const groups = groupSteps(steps);
    expect(groups.map((g) => g.kind)).toEqual(['run', 'step', 'run']);
    const transition = groups[1];
    if (transition.kind !== 'step') throw new Error('expected the transition to stay visible');
    expect(transition.step.to_state).toBe('clearing');
  });

  it('does not collapse confirmations that sit in different states', () => {
    const steps = [
      step({ sequence: 1, to_state: 'active', from_state: 'active' }),
      step({ sequence: 2, to_state: 'clearing', from_state: 'clearing' }),
    ];
    expect(groupSteps(steps).map((g) => g.kind)).toEqual(['step', 'step']);
  });

  it('leaves a lone confirmation as its own row', () => {
    expect(groupSteps([step()]).map((g) => g.kind)).toEqual(['step']);
  });
});

describe('visibleChanges', () => {
  const changes: Change[] = [
    { path: 'sources[ok/1].raw_ref', from: 'a', to: 'b', bookkeeping: true, notable: false },
    { path: 'confidence.value', from: 0.6, to: 0.61, bookkeeping: true, notable: false },
    { path: 'end_time', from: null, to: '2026-08-14T00:00:00Z', bookkeeping: false, notable: true },
  ];

  it('hides bookkeeping by default and reports how much it hid', () => {
    const { shown, hidden } = visibleChanges(changes, false);
    expect(shown.map((c) => c.path)).toEqual(['end_time']);
    // Counted, not dropped: a version with only bookkeeping changes must not read as
    // a version with no changes, which would make its existence unexplained.
    expect(hidden).toBe(2);
  });

  it('shows everything when asked', () => {
    expect(visibleChanges(changes, true).shown).toHaveLength(3);
    expect(visibleChanges(changes, true).hidden).toBe(0);
  });
});

describe('sortChanges', () => {
  it('leads with notable changes', () => {
    const sorted = sortChanges([
      { path: 'zzz', from: 1, to: 2, bookkeeping: false, notable: false },
      { path: 'lifecycle_state', from: 'a', to: 'b', bookkeeping: false, notable: true },
    ]);
    expect(sorted[0].path).toBe('lifecycle_state');
  });
});

describe('stateBands', () => {
  function span(state: string, seconds: number | null): StateSpan {
    return {
      state,
      entered_at: '2026-08-13T12:00:00.000Z',
      exited_at: null,
      first_step: 1,
      last_step: 1,
      updates: 0,
      trigger_in: null,
      seconds,
      current: false,
    };
  }

  it('gives a very short state a visible width', () => {
    // 4 seconds against 3 days is 0.0015% - which rounds to nothing, and a band
    // chart missing its first state says the record was never reported.
    const bands = stateBands([span('reported', 4), span('active', 259200)]);
    expect(bands[0].percent).toBeGreaterThanOrEqual(4);
    expect(bands[1].percent).toBeGreaterThan(bands[0].percent);
  });

  it('still sums to 100 percent', () => {
    const bands = stateBands([span('reported', 4), span('validated', 60), span('active', 259200)]);
    const total = bands.reduce((sum, band) => sum + band.percent, 0);
    expect(total).toBeCloseTo(100, 6);
  });

  it('falls back to equal widths when no duration is known', () => {
    const bands = stateBands([span('reported', null), span('active', null)]);
    expect(bands.map((b) => b.percent)).toEqual([50, 50]);
  });

  it('returns nothing for no spans rather than dividing by zero', () => {
    expect(stateBands([])).toEqual([]);
  });

  it('shrinks the floor rather than exceeding 100 percent with many spans', () => {
    const spans = Array.from({ length: 40 }, (_, i) => span(`s${i}`, 10));
    const bands = stateBands(spans);
    expect(bands.reduce((sum, b) => sum + b.percent, 0)).toBeCloseTo(100, 6);
  });
});

describe('findings', () => {
  const findings: Finding[] = [
    { code: 'single_witness', severity: 'info', detail: '' },
    { code: 'ttl_expired_not_moved', severity: 'error', detail: '' },
    { code: 'source_behind_slo', severity: 'warn', detail: '' },
  ];

  it('reports the worst severity present', () => {
    expect(worstSeverity(findings)).toBe('error');
    expect(worstSeverity([findings[0]])).toBe('info');
    expect(worstSeverity([])).toBeNull();
  });

  it('sorts errors above warnings above info', () => {
    expect(sortFindings(findings).map((f) => f.severity)).toEqual(['error', 'warn', 'info']);
  });
});

describe('filterRecords', () => {
  const rows = [
    row(),
    row({
      event_id: '01OTHER',
      event_class: 'incident',
      source_ids: ['az511-events'],
      agencies: ['Arizona DOT'],
      native_ids: ['9911'],
      states: ['AZ'],
      ttl_expired: true,
    }),
  ];

  it('matches an agency record id pasted from a 511 site', () => {
    expect(filterRecords(rows, { q: '250182-1' }).map((r) => r.event_id)).toEqual([
      rows[0].event_id,
    ]);
  });

  it('matches a state code as a substring', () => {
    expect(filterRecords(rows, { q: 'az' })).toHaveLength(1);
  });

  it('narrows by class and source together', () => {
    expect(filterRecords(rows, { classes: ['incident'], sources: ['az511-events'] })).toHaveLength(1);
    expect(filterRecords(rows, { classes: ['incident'], sources: ['ok-odot-wzdx'] })).toHaveLength(0);
  });

  it('can show only the records with a visible problem', () => {
    const suspect = filterRecords(rows, { onlyProblems: true });
    expect(suspect).toHaveLength(1);
    expect(suspect[0].ttl_expired).toBe(true);
  });

  it('returns everything for an empty query', () => {
    expect(filterRecords(rows, { q: '   ' })).toHaveLength(2);
  });
});

describe('isSuspect', () => {
  it('flags a lapsed TTL and an unresolved extent', () => {
    expect(isSuspect(row({ ttl_expired: true }))).toBe(true);
    expect(isSuspect(row({ unresolved_extent: true }))).toBe(true);
  });

  it('does not flag low confidence, which is the system working', () => {
    expect(isSuspect(row({ confidence: 0.2 }))).toBe(false);
  });
});

describe('ttlProgress', () => {
  it('is near zero just after an update and 100 once expired', () => {
    expect(ttlProgress(row({ quiet_seconds: 0, seconds_until_ttl: 600 }))).toBe(0);
    expect(ttlProgress(row({ quiet_seconds: 600, seconds_until_ttl: 0 }))).toBe(100);
  });

  it('is null when there is no TTL to be measured against', () => {
    // A dimensional_restriction in `active` has a TTL measured in years; a terminal
    // state has none at all. Both must render as "nothing to wait for".
    expect(ttlProgress(row({ seconds_until_ttl: null }))).toBeNull();
  });
});

describe('quietLevel', () => {
  it('is stale once the TTL has lapsed', () => {
    expect(quietLevel(row({ ttl_expired: true }))).toBe('stale');
  });

  it('is fresh right after an update and aging near the deadline', () => {
    expect(quietLevel(row({ quiet_seconds: 10, seconds_until_ttl: 890 }))).toBe('fresh');
    expect(quietLevel(row({ quiet_seconds: 880, seconds_until_ttl: 20 }))).toBe('aging');
  });
});

describe('extentLabel', () => {
  it('renders a state milepost range, which is what an agency recognizes', () => {
    expect(extentLabel(row())).toBe('OK MP 12.3-13.0');
  });

  it('collapses a point event to one milepost', () => {
    expect(
      extentLabel(
        row({ milepost_begin: { state: 'AZ', milepost: 229.4 }, milepost_end: { state: 'AZ', milepost: 229.4 } }),
      ),
    ).toBe('AZ MP 229.4');
  });

  it('names both states when the extent crosses a state line', () => {
    // One event spans the line rather than becoming two.
    expect(
      extentLabel(
        row({
          milepost_begin: { state: 'TX', milepost: 176.0 },
          milepost_end: { state: 'OK', milepost: 2.0 },
        }),
      ),
    ).toBe('TX MP 176.0 - OK MP 2.0');
  });

  it('says unresolved rather than showing a fabricated position', () => {
    expect(extentLabel(row({ unresolved_extent: true }))).toBe('unresolved');
    expect(
      extentLabel(row({ begin_measure: null, milepost_begin: null, milepost_end: null })),
    ).toBe('unresolved');
  });

  it('falls back to corridor measures when the corridor is unavailable', () => {
    expect(extentLabel(row({ milepost_begin: null, milepost_end: null }))).toBe(
      'measure 1100.2-1100.9',
    );
  });
});

describe('formatting helpers', () => {
  it('renders bytes at a readable scale', () => {
    expect(humanBytes(512)).toBe('512 B');
    expect(humanBytes(258808)).toBe('252.7 KB');
    expect(humanBytes(4284445)).toBe('4.1 MB');
    expect(humanBytes(null)).toBe('-');
  });

  it('shortens an s3 ref to the fetch that produced it', () => {
    expect(
      refLabel('s3://amzn-s3-demo-rawzone/raw/source=ok-odot-wzdx/year=2026/2026-08-13T21:53:21.006Z-34f6.json'),
    ).toBe('2026-08-13T21:53:21.006Z-34f6.json');
    expect(refLabel(null)).toBe('-');
  });
});
