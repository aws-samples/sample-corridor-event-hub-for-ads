/**
 * The tracker's pure logic: everything that decides WHAT is shown, separated from
 * the components that draw it.
 *
 * Here rather than inline in the JSX because these are the parts that can be
 * quietly wrong. A trace of 200 steps where 197 are re-fetch confirmations is
 * unreadable unless the runs collapse; a band chart of state durations where a
 * 4-second `reported` span renders as zero pixels tells you the record was never
 * reported. Both are arithmetic, and arithmetic in a component is arithmetic nobody
 * tests.
 *
 * ui/src/layout.ts makes the same split for the strip, for the same reason.
 */

import type { Change, Finding, RecordRow, Severity, Step, StateSpan } from './types';

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

/**
 * A duration a human can read at a glance, with the unit changing at the point the
 * previous one stops being informative. "5400s" and "1.5h" are the same fact and
 * only one of them is legible in a table.
 */
export function humanDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '-';
  const value = Math.max(0, seconds);
  if (value < 1) return '<1s';
  if (value < 90) return `${Math.round(value)}s`;
  if (value < 5400) return `${Math.round(value / 60)}m`;
  if (value < 172800) return `${(value / 3600).toFixed(1)}h`;
  return `${(value / 86400).toFixed(1)}d`;
}

/** Clock time, seconds included: the ordering of steps inside one second matters. */
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return '-';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  return new Date(parsed).toISOString().replace('T', ' ').replace('Z', 'Z').slice(0, 23);
}

export function secondsSince(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return null;
  return Math.max(0, (now - parsed) / 1000);
}

/**
 * Freshness banding for a record's quiet time. Thresholds are deliberately per
 * record rather than global: what "quiet" means depends on the class, so this bands
 * against the record's own TTL when there is one.
 */
export function quietLevel(row: RecordRow): 'fresh' | 'aging' | 'stale' {
  if (row.ttl_expired) return 'stale';
  const quiet = row.quiet_seconds ?? 0;
  const ttl = row.seconds_until_ttl;
  if (ttl !== null && ttl <= 0) return 'stale';
  if (ttl !== null && quiet > 0) {
    const share = quiet / (quiet + ttl);
    return share > 0.75 ? 'aging' : 'fresh';
  }
  return quiet < 900 ? 'fresh' : 'aging';
}

// ---------------------------------------------------------------------------
// Findings
// ---------------------------------------------------------------------------

const SEVERITY_RANK: Record<Severity, number> = { error: 3, warn: 2, info: 1 };

export function severityRank(severity: Severity): number {
  return SEVERITY_RANK[severity] ?? 0;
}

/** The worst finding on a record, or null. Drives the dot in the list. */
export function worstSeverity(findings: Finding[]): Severity | null {
  let worst: Severity | null = null;
  for (const finding of findings) {
    if (worst === null || severityRank(finding.severity) > severityRank(worst)) {
      worst = finding.severity;
    }
  }
  return worst;
}

/** Errors first. An `info` about corroboration must never sit above a dead timer. */
export function sortFindings(findings: Finding[]): Finding[] {
  return [...findings].sort((a, b) => severityRank(b.severity) - severityRank(a.severity));
}

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

/** A single interesting step, or a collapsed run of consecutive confirmations. */
export type StepGroup =
  | { kind: 'step'; step: Step }
  | {
      kind: 'run';
      count: number;
      first: Step;
      last: Step;
      state: string;
      /** Seconds from the first confirmation to the last, when both timestamps parse. */
      seconds: number | null;
    };

/**
 * Collapse consecutive confirmation-only steps into runs.
 *
 * WHY: live data has a record with 1,564 versions, 1,563 of which changed nothing
 * but `raw_ref` and `retrieved_at`. Rendered one per row, the two steps that
 * actually moved the record are invisible. Rendered as "1,563 confirmations over
 * 26h", they are the only thing on the screen.
 *
 * A run is only collapsed at length 2 or more - a single confirmation costs the same
 * space either way, and a "run of 1" reads as a summary of something bigger.
 */
export function groupSteps(steps: Step[], minRun = 2): StepGroup[] {
  const groups: StepGroup[] = [];
  let run: Step[] = [];

  const flush = () => {
    if (run.length === 0) return;
    if (run.length < minRun) {
      for (const step of run) groups.push({ kind: 'step', step });
    } else {
      const first = run[0];
      const last = run[run.length - 1];
      groups.push({
        kind: 'run',
        count: run.length,
        first,
        last,
        state: last.to_state,
        seconds: spanSeconds(first.recorded_at, last.recorded_at),
      });
    }
    run = [];
  };

  for (const step of steps) {
    if (step.confirmation_only) {
      // A run only continues while the state is unchanged; a confirmation in a
      // different state starts a new one, so a collapsed run never spans a
      // transition it would have hidden.
      if (run.length > 0 && run[run.length - 1].to_state !== step.to_state) flush();
      run.push(step);
      continue;
    }
    flush();
    groups.push({ kind: 'step', step });
  }
  flush();
  return groups;
}

function spanSeconds(from: string, to: string): number | null {
  const start = Date.parse(from);
  const end = Date.parse(to);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return Math.abs(end - start) / 1000;
}

/**
 * The diff lines to show for a step.
 *
 * Bookkeeping is hidden by default and COUNTED rather than dropped: "3 bookkeeping
 * changes hidden" is a fact about the version, and a step that rendered as having no
 * changes at all would misrepresent why the version exists.
 */
export function visibleChanges(
  changes: Change[],
  showBookkeeping: boolean,
): { shown: Change[]; hidden: number } {
  if (showBookkeeping) return { shown: changes, hidden: 0 };
  const shown = changes.filter((c) => !c.bookkeeping);
  return { shown, hidden: changes.length - shown.length };
}

/** Notable changes first, then the rest, each block in path order. */
export function sortChanges(changes: Change[]): Change[] {
  return [...changes].sort((a, b) => {
    if (a.notable !== b.notable) return a.notable ? -1 : 1;
    return a.path.localeCompare(b.path);
  });
}

// ---------------------------------------------------------------------------
// The state band
// ---------------------------------------------------------------------------

export interface Band {
  span: StateSpan;
  /** Percentage width, floored so a short state is visible rather than 0px wide. */
  percent: number;
}

/**
 * State spans as proportional widths.
 *
 * THE FLOOR IS THE POINT. A record that spent four seconds in `reported` and three
 * days in `active` has a first span worth 0.002% of the width - which rounds to
 * nothing, and a band chart missing its first state says the record was never
 * reported. So every span gets at least `minPercent`, and the rest is distributed
 * proportionally over what is left. The strip does the same thing with a 4px bar
 * floor, and says so in its legend; this one is stated in the caption.
 */
export function stateBands(spans: StateSpan[], minPercent = 4): Band[] {
  if (spans.length === 0) return [];
  const seconds = spans.map((s) => Math.max(0, s.seconds ?? 0));
  const total = seconds.reduce((sum, value) => sum + value, 0);

  // No measurable duration anywhere (every span sub-second, or all timestamps
  // unparseable): equal widths, which is honest about knowing nothing about the
  // proportions rather than showing one arbitrary span at full width.
  if (total <= 0) {
    return spans.map((span) => ({ span, percent: 100 / spans.length }));
  }

  const floor = Math.min(minPercent, 100 / spans.length);
  const free = 100 - floor * spans.length;
  return spans.map((span, index) => ({
    span,
    percent: floor + (seconds[index] / total) * free,
  }));
}

// ---------------------------------------------------------------------------
// The record list
// ---------------------------------------------------------------------------

/**
 * Client-side narrowing of an already-fetched list.
 *
 * Duplicated from the server's filter on purpose, and it is not a second source of
 * truth: the server filter bounds what is READ, this one makes typing feel
 * immediate without a round trip per keystroke against DynamoDB. Both narrow; if
 * they ever disagree the visible result is a subset, never a wrong row.
 */
export function filterRecords(
  rows: RecordRow[],
  filters: { q?: string; classes?: string[]; sources?: string[]; onlyProblems?: boolean },
): RecordRow[] {
  const needle = (filters.q ?? '').trim().toLowerCase();
  const classes = new Set(filters.classes ?? []);
  const sources = new Set(filters.sources ?? []);

  return rows.filter((row) => {
    if (classes.size > 0 && !classes.has(row.event_class)) return false;
    if (sources.size > 0 && !row.source_ids.some((id) => sources.has(id))) return false;
    if (filters.onlyProblems && !isSuspect(row)) return false;
    if (needle.length === 0) return true;
    return haystack(row).some((value) => value.toLowerCase().includes(needle));
  });
}

function haystack(row: RecordRow): string[] {
  return [
    row.event_id,
    row.event_class,
    row.event_subtype,
    row.lifecycle_state,
    ...row.native_ids,
    ...row.agencies,
    ...row.source_ids,
    ...row.states,
  ].map((value) => String(value ?? ''));
}

/**
 * Whether a row is worth a second look WITHOUT opening its trace.
 *
 * Deliberately narrow: only the two conditions visible in a summary that mean the
 * pipeline may have failed this record - a lapsed TTL nobody acted on and an
 * extent that never conflated, which no corridor query can return. Low
 * confidence is NOT here: a correctly-scored 0.4 is the system working.
 */
export function isSuspect(row: RecordRow): boolean {
  return row.ttl_expired || row.unresolved_extent;
}

/** Where a record sits between its last update and its TTL, as a percentage. */
export function ttlProgress(row: RecordRow): number | null {
  const quiet = row.quiet_seconds;
  const remaining = row.seconds_until_ttl;
  if (quiet === null || remaining === null) return null;
  const total = quiet + remaining;
  if (total <= 0) return 100;
  return Math.min(100, Math.max(0, (quiet / total) * 100));
}

/** A measure range as the milepost a DOT would recognize, falling back to measures. */
export function extentLabel(row: RecordRow): string {
  if (row.unresolved_extent) return 'unresolved';
  const begin = row.milepost_begin;
  const end = row.milepost_end;
  if (begin && end) {
    if (begin.state === end.state && Math.abs(begin.milepost - end.milepost) < 0.05) {
      return `${begin.state} MP ${begin.milepost.toFixed(1)}`;
    }
    if (begin.state === end.state) {
      return `${begin.state} MP ${begin.milepost.toFixed(1)}-${end.milepost.toFixed(1)}`;
    }
    return `${begin.state} MP ${begin.milepost.toFixed(1)} - ${end.state} MP ${end.milepost.toFixed(1)}`;
  }
  if (row.begin_measure === null) return 'unresolved';
  return `measure ${row.begin_measure.toFixed(1)}-${(row.end_measure ?? row.begin_measure).toFixed(1)}`;
}

/** Bytes, for the raw payload panel. */
export function humanBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '-';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The last path segment of an s3:// key - the part that identifies one fetch. */
export function refLabel(ref: string | null): string {
  if (!ref) return '-';
  const parts = ref.split('/');
  return parts[parts.length - 1] || ref;
}
