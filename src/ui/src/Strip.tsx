/**
 * The corridor strip.
 *
 * WHY A STRIP AND NOT A MAP: I-40 across four states is ~1,240 miles of nearly
 * straight east-west line. On a geographic map zoomed to fit the corridor, every
 * event collapses into an unreadable horizontal smear and most of the viewport is
 * empty desert. The canonical model already stores beginMeasure/endMeasure -
 * corridor miles from the western terminus - so 1-D is the data's native shape
 *. Measure on x, source on y, merged events on their own row: the gap
 * between the source rows and the MERGED row IS the dedup story.
 *
 * All geometry lives in layout.ts, which is pure and tested. This file is
 * rendering only - the two defects in the hand-written predecessor were both
 * geometry bugs, and keeping them separable is what let them be pinned by tests.
 *
 * Plain SVG rather than a charting library: it is rectangles on a linear scale,
 * and a library would be more code and more dependency surface than the chart.
 */

import { useMemo, useRef, useState } from 'react';
import {
  BAR_H,
  MERGE_GAP,
  PAD_L,
  PAD_R,
  PAD_T,
  ROW_PAD,
  LANE_H,
  W,
  assignLanes,
  barGeom,
  clusterToPlaceable,
  inView,
  laneY,
  measureAt,
  scaleX,
  ticks,
  toPlaceable,
  type Viewport,
} from './layout';
import { sourceNameWithId, sourceShortName } from './sourceNames';
import type { StripCandidate, StripData } from './types';

export interface Selection {
  kind: 'candidate' | 'cluster';
  id: number;
}

export interface TruckState {
  on: boolean;
  measure: number;
  direction: 'EB' | 'WB';
  lookAhead: number;
}

interface Props {
  data: StripData;
  view: Viewport;
  onViewChange: (v: Viewport) => void;
  selection: Selection | null;
  onSelect: (s: Selection) => void;
  classFilter: string;
  dirFilter: string;
  showMerges: boolean;
  showReview: boolean;
  truck: TruckState;
}

export function classColor(eventClass: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(`--c-${eventClass}`);
  return v.trim() || '#8b9bb4';
}

export function Strip(props: Props) {
  const {
    data,
    view,
    onViewChange,
    selection,
    onSelect,
    classFilter,
    dirFilter,
    showMerges,
    showReview,
    truck,
  } = props;

  const svgRef = useRef<SVGSVGElement>(null);
  const [drag, setDrag] = useState<{ from: number; to: number } | null>(null);

  const total = data.corridor.totalMiles;
  const byId = useMemo(() => {
    const m = new Map<number, StripCandidate>();
    for (const c of data.candidates) m.set(c.id, c);
    return m;
  }, [data.candidates]);

  const visible = (c: StripCandidate) =>
    (!classFilter || c.eventClass === classFilter) && (!dirFilter || c.direction === dirFilter);

  // --- layout ---------------------------------------------------------------
  const layout = useMemo(() => {
    let y = PAD_T;
    const rows = data.sources.map((src) => {
      const items = data.candidates
        .filter((c) => c.sourceId === src.sourceId)
        .map(toPlaceable)
        .filter((it) => inView(it.lo, it.hi, view));
      const laid = assignLanes(items, view);
      const h = laid.laneCount * LANE_H + ROW_PAD * 2;
      const row = { src, y, h, ...laid };
      y += h;
      return row;
    });

    const mergeY = y + MERGE_GAP;
    const clusterItems = data.clusters.map(clusterToPlaceable).filter((it) => inView(it.lo, it.hi, view));
    const mergeLaid = assignLanes(clusterItems, view);
    const mergeH = mergeLaid.laneCount * LANE_H + ROW_PAD * 2;

    return { rows, mergeY, mergeH, mergeLaid, bottom: mergeY + mergeH };
  }, [data, view]);

  const height = layout.bottom + 16;

  /** Painted centres, so merge links start from the right sub-lane. */
  const candPos = useMemo(() => {
    const m = new Map<number, { cx: number; cy: number; top: number }>();
    for (const row of layout.rows) {
      for (const p of row.placed) {
        const g = barGeom(p.lo, p.hi, view);
        const top = laneY(row.y, p.lane);
        m.set(p.item.id, {
          cx: g.x + g.w / 2,
          cy: top + BAR_H / 2,
          top,
        });
      }
    }
    return m;
  }, [layout, view]);

  // --- drag to zoom ---------------------------------------------------------
  const svgXFromEvent = (e: React.MouseEvent): number => {
    const rect = svgRef.current!.getBoundingClientRect();
    return ((e.clientX - rect.left) / rect.width) * W;
  };

  const onMouseDown = (e: React.MouseEvent) => {
    const x = svgXFromEvent(e);
    setDrag({ from: x, to: x });
  };
  const onMouseMove = (e: React.MouseEvent) => {
    if (!drag) return;
    setDrag({ ...drag, to: svgXFromEvent(e) });
  };
  const onMouseUp = () => {
    if (!drag) return;
    const lo = measureAt(Math.min(drag.from, drag.to), view);
    const hi = measureAt(Math.max(drag.from, drag.to), view);
    setDrag(null);
    // A click rather than a drag - zooming to a zero-width window would blank the
    // chart with no obvious way back.
    if (hi - lo < 0.5) return;
    onViewChange({ min: Math.max(0, lo), max: Math.min(total, hi) });
  };

  return (
    <svg
      ref={svgRef}
      viewBox={`0 0 ${W} ${height}`}
      role="img"
      aria-label="Corridor events by source and milepost"
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={() => setDrag(null)}
    >
      {/* state bands and labels */}
      {data.corridor.states.map((s, i) =>
        inView(s.beginMeasure, s.endMeasure, view) ? (
          <g key={s.state}>
            {i > 0 && s.beginMeasure > view.min && (
              <line
                x1={scaleX(s.beginMeasure, view)}
                y1={PAD_T - 16}
                x2={scaleX(s.beginMeasure, view)}
                y2={layout.bottom}
                className="state-line"
              />
            )}
            <text
              x={
                (Math.max(scaleX(s.beginMeasure, view), PAD_L) +
                  Math.min(scaleX(s.endMeasure, view), W - PAD_R)) /
                2
              }
              y={PAD_T - 20}
              className="state-label"
              textAnchor="middle"
            >
              {s.state}
            </text>
          </g>
        ) : null,
      )}

      {/* measure ticks */}
      {ticks(view).map((m) => (
        <g key={m}>
          <line
            x1={scaleX(m, view)}
            y1={PAD_T - 8}
            x2={scaleX(m, view)}
            y2={layout.bottom}
            className="grid-line"
            opacity={0.45}
          />
          <text x={scaleX(m, view)} y={PAD_T - 10} className="axis-text" textAnchor="middle">
            {m}
          </text>
        </g>
      ))}

      {/* zoom drag surface, behind the bars */}
      <rect
        x={PAD_L}
        y={PAD_T - 8}
        width={W - PAD_L - PAD_R}
        height={layout.bottom - PAD_T + 8}
        fill="transparent"
        style={{ cursor: 'crosshair' }}
        onMouseDown={onMouseDown}
      />

      {/* source rows */}
      {layout.rows.map((row, ri) => {
        const totalForRow = row.src.candidateCount;
        const shown = row.placed.length;
        const countText =
          shown === totalForRow
            ? `${totalForRow} ${totalForRow === 1 ? 'event' : 'events'}`
            : `${shown} of ${totalForRow} in view`;

        // An empty row is the most likely thing to be misread as a broken feed, so
        // it states its own reason. A source that fetched 200 OK and placed nothing
        // on the corridor is usually correct, not failing.
        const emptyWhy =
          shown > 0
            ? null
            : totalForRow > 0
              ? 'none in the current zoom window'
              : row.src.mode !== 'live'
                ? 'no live key - captured payload has none on this corridor'
                : row.src.offCorridor > 0
                  ? `${row.src.offCorridor} record(s) fetched, none on this corridor`
                  : 'feed returned nothing for this corridor';

        return (
          <g key={row.src.sourceId}>
            {ri % 2 === 1 && (
              <rect x={0} y={row.y} width={W} height={row.h} fill="rgba(255,255,255,0.015)" />
            )}
            <text x={PAD_L - 10} y={row.y + row.h / 2 - 1} className="row-label" textAnchor="end">
              <title>{sourceNameWithId(row.src.sourceId)}</title>
              {sourceShortName(row.src.sourceId)}
            </text>
            <text x={PAD_L - 10} y={row.y + row.h / 2 + 10} className="row-sub" textAnchor="end">
              {row.src.mode} &middot; {countText}
            </text>
            <line
              x1={PAD_L}
              y1={row.y + row.h}
              x2={W - PAD_R}
              y2={row.y + row.h}
              className="grid-line"
            />

            {/* Direction band labels, only where a row has more than one band -
                otherwise the ink implies a distinction that is not there. */}
            {row.bands.length > 1 &&
              row.bands
                .filter((b) => b.label)
                .map((b) => (
                  <text
                    key={b.key}
                    x={PAD_L + 4}
                    y={laneY(row.y, b.lane) + BAR_H - 3}
                    className="band-label"
                  >
                    {b.label}
                  </text>
                ))}

            {emptyWhy && (
              <text x={PAD_L + 12} y={row.y + row.h / 2 + 4} className="row-empty">
                {emptyWhy}
              </text>
            )}

            {row.placed.map((p) => {
              const c = p.item;
              const g = barGeom(p.lo, p.hi, view);
              const top = laneY(row.y, p.lane);
              const sel = selection?.kind === 'candidate' && selection.id === c.id;
              const color = classColor(c.eventClass);
              return (
                <g key={c.id}>
                  <rect
                    className={`bar${sel ? ' sel' : ''}${visible(c) ? '' : ' dim'}`}
                    data-kind="candidate"
                    data-id={c.id}
                    x={g.x}
                    y={top}
                    width={g.w}
                    height={BAR_H}
                    fill={color}
                    rx={2}
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelect({ kind: 'candidate', id: c.id });
                    }}
                  >
                    <title>{`${c.eventClass} ${c.direction}  ${c.beginLabel} -> ${c.endLabel}  conf ${c.confidence.value.toFixed(2)}`}</title>
                  </rect>
                  {/* Direction as a notch: colour already carries event class. */}
                  {(c.direction === 'EB' || c.direction === 'WB') && (
                    <path
                      d={
                        c.direction === 'EB'
                          ? `M${g.x + g.w} ${top} l5 ${BAR_H / 2} L${g.x + g.w} ${top + BAR_H} Z`
                          : `M${g.x} ${top} l-5 ${BAR_H / 2} L${g.x} ${top + BAR_H} Z`
                      }
                      fill={color}
                      opacity={0.85}
                      pointerEvents="none"
                    />
                  )}
                </g>
              );
            })}
          </g>
        );
      })}

      {/* merged row */}
      <text
        x={PAD_L - 10}
        y={layout.mergeY + layout.mergeH / 2 - 1}
        className="row-label merged"
        textAnchor="end"
      >
        MERGED
      </text>
      <text
        x={PAD_L - 10}
        y={layout.mergeY + layout.mergeH / 2 + 10}
        className="row-sub"
        textAnchor="end"
      >
        {data.clusters.length} events
      </text>
      <line
        x1={PAD_L}
        y1={layout.mergeY - MERGE_GAP / 2}
        x2={W - PAD_R}
        y2={layout.mergeY - MERGE_GAP / 2}
        className="grid-line"
        strokeDasharray="4 4"
      />

      {layout.mergeLaid.placed.map((p) => {
        const cl = p.item;
        const g = barGeom(p.lo, p.hi, view);
        const top = laneY(layout.mergeY, p.lane);
        const sel = selection?.kind === 'cluster' && selection.id === cl.clusterId;
        const anyVisible = cl.members.some((i) => {
          const c = byId.get(i);
          return c ? visible(c) : false;
        });
        const merged = cl.members.length > 1;
        return (
          <g key={cl.clusterId}>
            {/* Links from contributing records down to the merged bar. Only for
                real merges: with one member the vertical position already says
                which source it came from, so a link would be noise. */}
            {showMerges &&
              merged &&
              cl.members.map((mi) => {
                const pos = candPos.get(mi);
                if (!pos) return null;
                const tx = g.x + g.w / 2;
                return (
                  <path
                    key={mi}
                    className="merge-link"
                    d={`M${pos.cx} ${pos.top + BAR_H} C${pos.cx} ${pos.top + BAR_H + 20}, ${tx} ${top - 20}, ${tx} ${top}`}
                    pointerEvents="none"
                  />
                );
              })}
            <rect
              className={`bar${sel ? ' sel' : ''}${anyVisible ? '' : ' dim'}`}
              data-kind="cluster"
              data-id={cl.clusterId}
              x={g.x}
              y={top}
              width={g.w}
              height={BAR_H}
              fill={classColor(cl.eventClass)}
              rx={2}
              stroke={merged ? 'var(--good)' : undefined}
              strokeWidth={merged ? 1.5 : undefined}
              onClick={(e) => {
                e.stopPropagation();
                onSelect({ kind: 'cluster', id: cl.clusterId });
              }}
            >
              <title>{`event #${cl.clusterId} - ${cl.eventClass} - ${cl.agencies.join(' + ')} - conf ${cl.confidence.value.toFixed(2)}`}</title>
            </rect>
          </g>
        );
      })}

      {/* Ambiguous pairs, between the two candidate bars - the point is
          that they were NOT merged. Deduped: a pair is recorded on both clusters
          so neither side of the decision is invisible to a reviewer. */}
      {showReview &&
        (() => {
          const drawn = new Set<string>();
          const paths: React.ReactNode[] = [];
          for (const cl of data.clusters) {
            for (const pr of cl.reviewPairs) {
              const key = `${pr.from}-${pr.to}`;
              if (drawn.has(key)) continue;
              drawn.add(key);
              const a = candPos.get(pr.from);
              const b = candPos.get(pr.to);
              if (!a || !b) continue;
              const midY = (a.cy + b.cy) / 2 + (Math.abs(a.cy - b.cy) < 1 ? -13 : 0);
              paths.push(
                <path
                  key={key}
                  className="review-link"
                  d={`M${a.cx} ${a.cy} Q${(a.cx + b.cx) / 2} ${midY}, ${b.cx} ${b.cy}`}
                  pointerEvents="none"
                />,
              );
            }
          }
          return paths;
        })()}

      {/* look-ahead overlay */}
      {truck.on &&
        (() => {
          const tx = scaleX(truck.measure, view);
          const lo =
            truck.direction === 'EB' ? truck.measure : Math.max(0, truck.measure - truck.lookAhead);
          const hi =
            truck.direction === 'EB'
              ? Math.min(total, truck.measure + truck.lookAhead)
              : truck.measure;
          const d = truck.direction === 'EB' ? 1 : -1;
          return (
            <g pointerEvents="none">
              <rect
                className="truck-cone"
                x={scaleX(lo, view)}
                y={PAD_T - 6}
                width={scaleX(hi, view) - scaleX(lo, view)}
                height={layout.bottom - PAD_T + 6}
              />
              <line className="truck-line" x1={tx} y1={PAD_T - 12} x2={tx} y2={layout.bottom} />
              <path
                className="truck"
                d={`M${tx} ${PAD_T - 19} l${d * 9} 5 l${-d * 9} 5 Z`}
              />
            </g>
          );
        })()}

      {/* drag marker */}
      {drag && Math.abs(drag.to - drag.from) > 1 && (
        <rect
          x={Math.min(drag.from, drag.to)}
          y={PAD_T - 8}
          width={Math.abs(drag.to - drag.from)}
          height={layout.bottom - PAD_T + 8}
          fill="rgba(88,166,255,0.18)"
          pointerEvents="none"
        />
      )}

    </svg>
  );
}
