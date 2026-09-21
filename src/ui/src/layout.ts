/**
 * Strip geometry: pure functions, no React, no DOM.
 *
 * Extracted so the parts that were historically wrong can be TESTED. Two real
 * defects lived in the hand-written viewer this replaces, both invisible to
 * inspection and both caught only by poking the rendered page:
 *
 *   1. Overlapping bars drew exactly on top of each other, so the upper one was
 *      unclickable and the lower one invisible - the strip silently under-reported.
 *   2. The first fix compared MEASURES with a tolerance of `span * 0.006`, which is
 *      7.45 miles at full-corridor zoom. Two Texas work zones 3 miles apart were
 *      pushed onto separate rows despite being visibly separate, which is where the
 *      phantom "4 rows for Texas" came from.
 *
 * The lesson encoded here: an overlap test must be in the units of the thing it
 * prevents. Pixel overlap is the problem, so compare painted pixel extents.
 */

import type { Direction, StripCandidate, StripCluster } from './types';

export type { Direction };

/** viewBox units. The SVG scales to its container; these never change. */
export const W = 1040;
/**
 * Left gutter. Sized to the widest row label rather than to the ids it used to
 * hold: the rows are named after their feeds now (see sourceNames.ts), and
 * "Amazon Location traffic" needs ~130px at the 11px label size. Everything
 * downstream derives from PLOT_W, so widening this costs plot width and nothing
 * else - a clipped source name is worse than 20 fewer pixels of axis.
 */
export const PAD_L = 152;
export const PAD_R = 26;
export const PAD_T = 34;
export const PLOT_W = W - PAD_L - PAD_R;

export const LANE_H = 17;
export const BAR_H = 13;
export const ROW_PAD = 7;
export const MERGE_GAP = 18;

/**
 * Minimum painted bar width. A 0.5-mile event on a 1,240-mile axis rounds to
 * nothing, and an unclickable event is worse than an imprecise one - so bars have
 * a floor, and the UI says so rather than implying the width is to scale.
 *
 * Shared by the overlap test and the drawing code so the two cannot disagree
 * about how wide a bar actually is.
 */
export const MIN_BAR = 4;

export interface Viewport {
  min: number;
  max: number;
}

export function scaleX(measure: number, view: Viewport): number {
  const span = view.max - view.min || 1;
  return PAD_L + ((measure - view.min) / span) * PLOT_W;
}

/** Inverse of scaleX, for drag-to-zoom. */
export function measureAt(px: number, view: Viewport): number {
  const span = view.max - view.min || 1;
  return view.min + ((px - PAD_L) / PLOT_W) * span;
}

export function inView(lo: number, hi: number, view: Viewport): boolean {
  return hi >= view.min && lo <= view.max;
}

export interface BarGeom {
  x: number;
  w: number;
}

export function barGeom(lo: number, hi: number, view: Viewport): BarGeom {
  const x0 = scaleX(lo, view);
  const x1 = scaleX(hi, view);
  return { x: x0, w: Math.max(x1 - x0, MIN_BAR) };
}

export interface Placeable {
  lo: number;
  hi: number;
  direction: Direction;
}

/**
 * A placeable carrying its payload. Generic in the payload so `placed[].item` is
 * correctly typed at the call site and no cast is needed - the earlier version
 * forced `as StripCandidate` in the renderer, which would have silently accepted
 * the wrong type.
 */
export interface WithItem<T> extends Placeable {
  item: T;
}

export interface Placed<T> {
  item: T;
  lo: number;
  hi: number;
  lane: number;
  direction: Direction;
}

export interface BandInfo {
  key: 'EB' | 'MID' | 'WB';
  label: string;
  lane: number;
  lanes: number;
}

export interface LaneLayout<T> {
  placed: Array<Placed<T>>;
  laneCount: number;
  bands: BandInfo[];
}

/**
 * Direction bands, top to bottom.
 *
 * Direction is the vertical axis because it reflects a fact about the data rather
 * than an artifact of drawing: agencies report ONE physical work zone as TWO
 * records, one per direction. Every stacked pair in the Oklahoma and Texas feeds
 * is an EB/WB pair of the same project, and they must stay distinct - for a truck
 * heading east, the westbound closure is not its problem, which is also why the
 * matcher's direction gate refuses to merge them.
 *
 * It also removes most collisions for free, since the colliding pairs were
 * opposing directions.
 */
const BANDS: Array<{ key: BandInfo['key']; label: string; match: (d: Direction) => boolean }> = [
  { key: 'EB', label: 'EB >', match: (d) => d === 'EB' },
  { key: 'MID', label: '', match: (d) => d !== 'EB' && d !== 'WB' },
  { key: 'WB', label: '< WB', match: (d) => d === 'WB' },
];

/**
 * Split into direction bands, then stack within a band only where the PAINTED
 * bars would actually touch.
 *
 * Residual stacking carries no meaning - it exists so nothing hides anything else.
 */
export function assignLanes<T>(items: Array<WithItem<T>>, view: Viewport): LaneLayout<T> {
  const placed: Array<Placed<T>> = [];
  const bands: BandInfo[] = [];
  let laneCursor = 0;

  for (const band of BANDS) {
    const mine = items
      .filter((it) => band.match(it.direction))
      .slice()
      .sort((a, b) => a.lo - b.lo);
    if (mine.length === 0) continue;

    // Painted right edges, in viewBox units, plus 2 units of breathing room.
    const laneEnds: number[] = [];
    for (const it of mine) {
      const left = scaleX(it.lo, view);
      const right = Math.max(scaleX(it.hi, view), left + MIN_BAR) + 2;
      let lane = 0;
      while (lane < laneEnds.length && laneEnds[lane] > left) lane++;
      laneEnds[lane] = right;
      placed.push({
        item: it.item,
        lo: it.lo,
        hi: it.hi,
        lane: laneCursor + lane,
        direction: it.direction,
      });
    }

    bands.push({ key: band.key, label: band.label, lane: laneCursor, lanes: laneEnds.length });
    laneCursor += laneEnds.length;
  }

  return { placed, laneCount: Math.max(1, laneCursor), bands };
}

export function laneY(rowY: number, lane: number): number {
  return rowY + ROW_PAD + lane * LANE_H;
}

/** Tick spacing that stays readable at every zoom level. */
export function tickStep(span: number): number {
  if (span > 800) return 100;
  if (span > 400) return 50;
  if (span > 150) return 25;
  if (span > 60) return 10;
  if (span > 20) return 5;
  return 1;
}

export function ticks(view: Viewport): number[] {
  const step = tickStep(view.max - view.min);
  const first = Math.ceil(view.min / step) * step;
  const out: number[] = [];
  for (let m = first; m <= view.max; m += step) out.push(m);
  return out;
}

/**
 * Which entry the corridor/state zoom dropdown should be showing for a given view.
 *
 * The dropdown used to be hardcoded to `value=""`, which broke it two ways: it
 * always read "full corridor" no matter where the strip was zoomed, and picking
 * "full corridor" to zoom back out fired NO change event, because that was already
 * the element's value. Deriving the value from the viewport is what makes the
 * control report the state it is in - and `ZOOM_CUSTOM` is needed because a
 * drag-zoom lands on a window that is neither a state nor the whole corridor, and
 * a select forced to `""` in that case would claim "full corridor" while showing a
 * 30-mile slice of Texas.
 */
export const ZOOM_CUSTOM = 'custom';

/** Miles. Wider than float noise, far narrower than the shortest state. */
const ZOOM_EPS = 0.05;

export function zoomValue(
  view: Viewport,
  states: Array<{ state: string; beginMeasure: number; endMeasure: number }>,
  total: number,
): string {
  const near = (a: number, b: number) => Math.abs(a - b) <= ZOOM_EPS;
  if (near(view.min, 0) && near(view.max, total)) return '';
  const hit = states.find((s) => near(view.min, s.beginMeasure) && near(view.max, s.endMeasure));
  return hit ? hit.state : ZOOM_CUSTOM;
}

export function toPlaceable(c: StripCandidate): WithItem<StripCandidate> {
  return {
    item: c,
    lo: Math.min(c.beginMeasure, c.endMeasure),
    hi: Math.max(c.beginMeasure, c.endMeasure),
    direction: c.direction,
  };
}

export function clusterToPlaceable(cl: StripCluster): WithItem<StripCluster> {
  return {
    item: cl,
    lo: Math.min(cl.beginMeasure, cl.endMeasure),
    hi: Math.max(cl.beginMeasure, cl.endMeasure),
    direction: cl.direction,
  };
}

/**
 * "What is ahead of me", the automated-truck query.
 *
 * Mirrors `is_ahead_of` in `corridor_event_hub/core/lrs.py`. Duplicated because the UI
 * cannot import Python; `lrs.py` is authoritative - it is the one with tests
 * against the corridor config. Kept to the same shape so a divergence is obvious
 * on inspection.
 */
export function isAhead(
  event: { beginMeasure: number; endMeasure: number; direction: Direction },
  position: { measure: number; direction: Direction },
  lookAheadMiles: number,
): boolean {
  if (
    event.direction !== 'BOTH' &&
    event.direction !== 'UNKNOWN' &&
    position.direction !== 'BOTH' &&
    event.direction !== position.direction
  ) {
    return false;
  }
  const lo = Math.min(event.beginMeasure, event.endMeasure);
  const hi = Math.max(event.beginMeasure, event.endMeasure);
  return position.direction === 'WB'
    ? lo <= position.measure && hi >= position.measure - lookAheadMiles
    : hi >= position.measure && lo <= position.measure + lookAheadMiles;
}

/** Distance from a position to an event, along the direction of travel. */
export function distanceAhead(
  event: { beginMeasure: number; endMeasure: number },
  position: { measure: number; direction: Direction },
): number {
  const lo = Math.min(event.beginMeasure, event.endMeasure);
  const hi = Math.max(event.beginMeasure, event.endMeasure);
  return position.direction === 'WB'
    ? Math.max(0, position.measure - hi)
    : Math.max(0, lo - position.measure);
}
