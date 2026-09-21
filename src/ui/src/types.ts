/**
 * The wire format served by `/api/strip`.
 *
 * This mirrors what `corridor_event_hub/strip_export.py::build()` emits, which is
 * deliberately close to what the query API must return - so pointing
 * this app at the real API later is a base-URL change, not a rewrite.
 *
 * These are hand-written rather than generated. A generator would be the right
 * answer once the API is real and has a schema; today the producer is one Python
 * function and a codegen step would be more machinery than contract.
 */

export type Direction = 'EB' | 'WB' | 'BOTH' | 'UNKNOWN';

/** How the exporter obtained a source's bytes. Never inferred, always reported. */
export type SourceMode = 'live' | 'fixture' | 'failed';

export interface StripIssue {
  reason: string;
  field: string;
  count: number;
  example: string | null;
}

export interface StripSource {
  sourceId: string;
  agency: string;
  label: string;
  mode: SourceMode;
  httpStatus: number | null;
  latencyMs: number | null;
  payloadBytes: number | null;
  candidateCount: number;
  offCorridor: number;
  issues: StripIssue[];
  note: string | null;

  /**
   * Licence terms, carried with the data because the strip is the artifact someone
   * screenshots into a deck. `null` means UNKNOWN, which is not the same as
   * permitted.
   */
  redistributable: boolean | null;
  licenseShort: string | null;
  attribution: string | null;

  /**
   * Catalog facts the timeline grades trust against.
   *
   * `independenceGroup` is the one that changes a conclusion: sources sharing a
   * group corroborate ONCE, so a two-agency event can still be
   * single-source evidence.
   *
   * `snapshotSemantics` is 'cleared' where the agency has confirmed that a record
   * leaving a snapshot means the event ended, and 'UNKNOWN' until someone asks.
   * UNKNOWN is why disappearance routes to `clearing`, never `cleared`.
   *
   * Cadence and SLO make staleness a per-feed judgement: an hour of silence is
   * nothing from a daily work-zone feed and a fault from a 60-second one.
   */
  independenceGroup: string | null;
  snapshotSemantics: string | null;
  publishCadenceSeconds: number | null;
  freshnessSloSeconds: number | null;
}

export interface ConfidenceOut {
  value: number;
  breakdown: Record<string, number>;
  explanation: string[];
}

export interface LaneImpact {
  ordinal: number;
  type: string;
  status: string;
  inferred: boolean;
}

export interface StripCandidate {
  id: number;
  sourceId: string;
  agency: string;
  nativeId: string;
  eventClass: string;
  eventSubtype: string;
  beginMeasure: number;
  endMeasure: number;
  direction: Direction;
  states: string[];
  beginLabel: string;
  endLabel: string;
  conflationMethod: string;
  positionalAccuracyMeters: number | null;
  startTime: string;
  endTime: string | null;
  timeConfidence: string;
  /**
   * The two system clocks, kept apart. `sourceUpdatedAt` is when the AGENCY
   * last changed the record - what recency decays from; `retrievedAt` is
   * when we fetched, which is always ~now. The gap between them is the single most
   * useful trust signal on this record, and reading the wrong one once made the
   * strip score a three-week-old work zone as fresh.
   */
  sourceUpdatedAt: string | null;
  retrievedAt: string;
  laneImpacts: LaneImpact[];
  agencySeverity: string | null;
  confidence: ConfidenceOut;
  rawRef: string;
  issueCount: number;
}

export interface MatchPairOut {
  from: number;
  to: number;
  value: number;
  explanation: string[];
}

/** One legal edge out of the event's current state, as published data. */
export interface TransitionOut {
  toState: string;
  triggers: string[];
  rationale: string;
}

/** Where a disappearance from one contributing feed would send this event. */
export interface SourceAbsentOut {
  sourceId: string;
  snapshotSemantics: string | null;
  toState: string;
  reason: string;
}

/**
 * The lifecycle position of an event: where it is, when its timer fires, and where
 * it can legally go. The transition table is served rather than hardcoded here on
 * purpose - a copy in TypeScript would drift from `core/lifecycle.py` the first time
 * an edge changed, and would do it silently.
 */
export interface LifecycleOut {
  enteredAt: string;
  ttlExpiresAt: string | null;
  /** The recency basis the confidence score above actually used. */
  lastConfirmedAt: string;
  reopenWindowSeconds: number;
  confidenceHalfLifeSeconds: number;
  transitions: TransitionOut[];
  sourceAbsent: SourceAbsentOut[];
  /**
   * False from this exporter, which holds no state between builds. Elapsed time in
   * a state is therefore NOT derivable from this document, and the UI must not
   * present `enteredAt` as when the event began.
   */
  historyAvailable: boolean;
  note: string;
}

export interface StripCluster {
  clusterId: number;
  members: number[];
  beginMeasure: number;
  endMeasure: number;
  eventClass: string;
  direction: Direction;
  agencies: string[];
  confidence: ConfidenceOut;
  joins: MatchPairOut[];
  reviewPairs: MatchPairOut[];
  lifecycleState: string;
  ttlSeconds: number | null;
  lifecycle: LifecycleOut;
}

export interface CorridorState {
  state: string;
  beginMeasure: number;
  endMeasure: number;
}

export interface StripCorridor {
  route: string;
  totalMiles: number;
  verified: boolean;
  warning: string | null;
  states: CorridorState[];
}

/**
 * Cache metadata from the dev API. Describes the SERVER's cache, not the corridor:
 * `servedFromCache` distinguishes "re-ran the adapters" from "replayed the same
 * bytes", which are different claims and must not look identical in the UI.
 */
export interface CacheMeta {
  servedFromCache: boolean;
  cacheAgeSeconds: number;
  minRefreshSeconds: number;
  buildCount: number;
  fixturesOnly: boolean;
  refreshThrottled: boolean;
}

/**
 * The scoring model itself, published because an integrator sets a trust threshold
 * against its output. The weights are also what let the viewer
 * project confidence forward: recency is the only component that moves with the clock
 *, so its weight is needed to say anything about the total.
 */
export interface ConfidenceModelOut {
  version: string;
  weights: Record<string, number>;
}

export interface StripData {
  generatedAt: string;
  corridor: StripCorridor;
  matchModelVersion: string;
  confidenceModel: ConfidenceModelOut;
  sources: StripSource[];
  candidates: StripCandidate[];
  clusters: StripCluster[];
  unsourcedClasses: Array<{ eventClass: string; reason: string }>;
  /** Absent when reading a static export rather than the dev API. */
  cache?: CacheMeta;
}
