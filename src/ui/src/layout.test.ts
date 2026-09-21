/**
 * Layout tests - these exist because of two real defects in the viewer this
 * replaces, neither of which was visible by reading the code.
 *
 * Both were found by poking the rendered page (a headless click that could not
 * reach a bar; a user asking "why does Texas have four rows?"). Encoding them here
 * is the difference between a fix and a regression test.
 */

import { describe, it, expect } from 'vitest';
import {
  assignLanes,
  barGeom,
  distanceAhead,
  inView,
  isAhead,
  laneY,
  measureAt,
  scaleX,
  tickStep,
  ticks,
  zoomValue,
  MIN_BAR,
  ZOOM_CUSTOM,
  PAD_L,
  PLOT_W,
  type Direction,
  type Viewport,
  type WithItem,
} from './layout';

const FULL: Viewport = { min: 0, max: 1241 };

/** A minimal placeable; the payload is just a label so failures are readable. */
function item(lo: number, hi: number, direction: Direction = 'EB'): WithItem<string> {
  return { item: `${direction}:${lo}-${hi}`, lo, hi, direction };
}

describe('scale', () => {
  it('maps the view window onto the plot area', () => {
    expect(scaleX(0, FULL)).toBeCloseTo(PAD_L);
    expect(scaleX(1241, FULL)).toBeCloseTo(PAD_L + PLOT_W);
  });

  it('round-trips through measureAt', () => {
    for (const m of [0, 100, 620.5, 1241]) {
      expect(measureAt(scaleX(m, FULL), FULL)).toBeCloseTo(m, 4);
    }
  });

  it('does not divide by zero on a degenerate window', () => {
    expect(Number.isFinite(scaleX(5, { min: 5, max: 5 }))).toBe(true);
  });

  it('gives a point event a clickable minimum width', () => {
    // A 0-length event at full corridor zoom would otherwise be 0px wide.
    expect(barGeom(177.7, 177.7, FULL).w).toBe(MIN_BAR);
  });

  it('scales a long event to its real width when that exceeds the floor', () => {
    const g = barGeom(0, 620.5, FULL);
    expect(g.w).toBeCloseTo(PLOT_W / 2, 0);
  });
});

describe('inView', () => {
  it('includes events partly overlapping the window', () => {
    expect(inView(700, 800, { min: 750, max: 900 })).toBe(true);
    expect(inView(700, 800, { min: 600, max: 750 })).toBe(true);
  });

  it('excludes events entirely outside it', () => {
    expect(inView(100, 200, { min: 700, max: 900 })).toBe(false);
  });
});

describe('direction bands (the "why does Texas have 4 rows" bug)', () => {
  it('puts EB above WB, in separate bands', () => {
    const out = assignLanes(
      [item(789, 790.2, 'EB'), item(789, 790.2, 'WB')],
      FULL,
    );
    // Two bands, one lane each - NOT two lanes in one band.
    expect(out.laneCount).toBe(2);
    expect(out.bands.map((b) => b.key)).toEqual(['EB', 'WB']);

    const eb = out.placed.find((p) => p.direction === 'EB')!;
    const wb = out.placed.find((p) => p.direction === 'WB')!;
    expect(laneY(0, eb.lane)).toBeLessThan(laneY(0, wb.lane));
  });

  it('collapses the real Texas feed to two rows once zoomed enough to separate it', () => {
    // The real TxDOT records: two work zones, each reported once per direction.
    //
    // At FULL-corridor zoom these genuinely do collide in painted pixels - bar A
    // paints 692.8->698.8 and bar B starts at 695.6 - so stacking to 4 lanes is
    // CORRECT there, and forcing 2 would hide two events. The old bug was not that
    // it stacked here; it was that it also stacked at TX zoom, where the bars are
    // 12px apart and visibly distinct.
    const tx = [
      item(789.0, 790.2, 'EB'),
      item(789.0, 790.2, 'WB'),
      item(793.0, 793.6, 'EB'),
      item(793.0, 793.6, 'WB'),
    ];

    // Zoomed to Texas: two bands, one lane each. This is the assertion that would
    // have caught the original defect.
    expect(assignLanes(tx, { min: 733, max: 910 }).laneCount).toBe(2);

    // At full zoom it stacks, and that is the honest answer rather than overlap.
    expect(assignLanes(tx, FULL).laneCount).toBe(4);
  });

  it('keeps the real Oklahoma feed to exactly two rows', () => {
    const out = assignLanes(
      [item(956.9, 960.3, 'EB'), item(957.5, 960.2, 'WB')],
      FULL,
    );
    expect(out.laneCount).toBe(2);
  });

  it('centres BOTH and UNKNOWN between the directional bands', () => {
    const out = assignLanes(
      [item(100, 200, 'EB'), item(100, 200, 'BOTH'), item(100, 200, 'WB')],
      FULL,
    );
    expect(out.bands.map((b) => b.key)).toEqual(['EB', 'MID', 'WB']);
    const mid = out.placed.find((p) => p.direction === 'BOTH')!;
    const eb = out.placed.find((p) => p.direction === 'EB')!;
    const wb = out.placed.find((p) => p.direction === 'WB')!;
    expect(mid.lane).toBeGreaterThan(eb.lane);
    expect(mid.lane).toBeLessThan(wb.lane);
  });

  it('labels only the directional bands', () => {
    const out = assignLanes([item(1, 2, 'BOTH')], FULL);
    expect(out.bands).toHaveLength(1);
    expect(out.bands[0].label).toBe('');
  });
});

describe('overlap stacking (the unclickable-bar bug)', () => {
  it('separates two events at the same milepost so neither hides the other', () => {
    const out = assignLanes([item(100, 101, 'EB'), item(100, 101, 'EB')], FULL);
    const lanes = out.placed.map((p) => p.lane);
    expect(new Set(lanes).size).toBe(2);
  });

  it('does NOT separate events that are visibly apart', () => {
    // 3 miles apart at full zoom is ~2px of gap, but the bars are 4px wide and
    // would touch. Zoomed into one state they are clearly separate, so they must
    // share a lane there - the test is about painted pixels, not miles.
    const zoomed: Viewport = { min: 733, max: 910 };
    const out = assignLanes([item(789, 790.2, 'EB'), item(793, 793.6, 'EB')], zoomed);
    expect(out.laneCount).toBe(1);
  });

  it('reuses a lane once a previous bar has ended', () => {
    const out = assignLanes(
      [item(0, 10, 'EB'), item(0, 10, 'EB'), item(1200, 1210, 'EB')],
      FULL,
    );
    // Third bar is far right; it fits back in the first lane.
    expect(out.laneCount).toBe(2);
    expect(out.placed.find((p) => p.lo === 1200)!.lane).toBe(0);
  });

  it('handles an empty input without producing a zero-height row', () => {
    const out = assignLanes([], FULL);
    expect(out.laneCount).toBe(1);
    expect(out.placed).toEqual([]);
    expect(out.bands).toEqual([]);
  });

  it('is deterministic regardless of input order', () => {
    const a = assignLanes([item(100, 110, 'EB'), item(105, 115, 'EB')], FULL);
    const b = assignLanes([item(105, 115, 'EB'), item(100, 110, 'EB')], FULL);
    expect(a.laneCount).toBe(b.laneCount);
  });
});

describe('ticks', () => {
  it('widens spacing as the window widens', () => {
    expect(tickStep(1241)).toBe(100);
    expect(tickStep(177)).toBe(25);
    expect(tickStep(10)).toBe(1);
  });

  it('stays inside the window and never returns an empty axis', () => {
    for (const view of [FULL, { min: 733, max: 910 }, { min: 800, max: 805 }]) {
      const t = ticks(view);
      expect(t.length).toBeGreaterThan(0);
      expect(Math.min(...t)).toBeGreaterThanOrEqual(view.min);
      expect(Math.max(...t)).toBeLessThanOrEqual(view.max);
    }
  });
});

describe('look-ahead', () => {
  const eb = { beginMeasure: 300, endMeasure: 310, direction: 'EB' as const };

  it('finds an event ahead in the direction of travel', () => {
    expect(isAhead(eb, { measure: 290, direction: 'EB' }, 25)).toBe(true);
  });

  it('ignores an event behind the truck', () => {
    expect(isAhead(eb, { measure: 320, direction: 'EB' }, 25)).toBe(false);
  });

  it('ignores an event beyond the look-ahead distance', () => {
    expect(isAhead(eb, { measure: 200, direction: 'EB' }, 25)).toBe(false);
  });

  it('does not report an opposing-direction event to the driver', () => {
    // The whole reason direction is modelled: a westbound closure is not an
    // eastbound truck's problem.
    expect(isAhead(eb, { measure: 290, direction: 'WB' }, 25)).toBe(false);
  });

  it('reports BOTH and UNKNOWN events to either direction', () => {
    const both = { ...eb, direction: 'BOTH' as const };
    const unknown = { ...eb, direction: 'UNKNOWN' as const };
    expect(isAhead(both, { measure: 290, direction: 'EB' }, 25)).toBe(true);
    expect(isAhead(both, { measure: 320, direction: 'WB' }, 25)).toBe(true);
    // UNKNOWN must not be silently dropped: most AZ511 directions resolve to it,
    // so treating it as a mismatch would hide most Arizona events from the query.
    expect(isAhead(unknown, { measure: 290, direction: 'EB' }, 25)).toBe(true);
  });

  it('works westbound, where measures decrease', () => {
    const wb = { beginMeasure: 300, endMeasure: 310, direction: 'WB' as const };
    expect(isAhead(wb, { measure: 320, direction: 'WB' }, 25)).toBe(true);
    expect(isAhead(wb, { measure: 290, direction: 'WB' }, 25)).toBe(false);
  });

  it('measures distance to the near edge, and zero when already inside', () => {
    expect(distanceAhead(eb, { measure: 290, direction: 'EB' })).toBe(10);
    expect(distanceAhead(eb, { measure: 305, direction: 'EB' })).toBe(0);
    expect(distanceAhead(eb, { measure: 330, direction: 'WB' })).toBe(20);
  });
});

describe('zoom dropdown value', () => {
  // The real corridor: measured offsets from reference/corridor.json. Note that
  // totalMiles is rounded to one decimal (1240.7) while OK's end measure is not
  // (1240.699) - the dropdown must not confuse the two.
  const STATES = [
    { state: 'AZ', beginMeasure: 0, endMeasure: 359.349 },
    { state: 'NM', beginMeasure: 359.349, endMeasure: 732.658 },
    { state: 'TX', beginMeasure: 732.658, endMeasure: 909.73 },
    { state: 'OK', beginMeasure: 909.73, endMeasure: 1240.699 },
  ];
  const TOTAL = 1240.7;

  it('reads as the full corridor when nothing is zoomed', () => {
    expect(zoomValue({ min: 0, max: TOTAL }, STATES, TOTAL)).toBe('');
  });

  it('reports the state the strip is actually zoomed to', () => {
    // The bug this replaces: the select was hardcoded to value="", so it read
    // "full corridor" while showing Texas, and re-picking "full corridor" fired no
    // change event at all - the dropdown could zoom in but never back out.
    expect(zoomValue({ min: 732.658, max: 909.73 }, STATES, TOTAL)).toBe('TX');
    expect(zoomValue({ min: 0, max: 359.349 }, STATES, TOTAL)).toBe('AZ');
  });

  it('does not mistake the last state for the whole corridor', () => {
    // OK ends 0.001 mi short of the rounded total, and starts at 909.73.
    expect(zoomValue({ min: 909.73, max: 1240.699 }, STATES, TOTAL)).toBe('OK');
  });

  it('reports a drag-zoom as custom rather than claiming a state', () => {
    expect(zoomValue({ min: 800, max: 830 }, STATES, TOTAL)).toBe(ZOOM_CUSTOM);
    // A window that shares one edge with a state is still not that state.
    expect(zoomValue({ min: 732.658, max: 800 }, STATES, TOTAL)).toBe(ZOOM_CUSTOM);
  });

  it('tolerates float noise, not a visible difference', () => {
    expect(zoomValue({ min: 732.658000001, max: 909.7299999 }, STATES, TOTAL)).toBe('TX');
    expect(zoomValue({ min: 732.658, max: 910.5 }, STATES, TOTAL)).toBe(ZOOM_CUSTOM);
  });
});
