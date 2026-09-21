/**
 * The wire format served by the trace API.
 *
 * snake_case, unlike ui/src/types.ts. That is not an inconsistency, it is which
 * API each app reads: the strip document is built for a browser and camelCases on
 * the way out, while these routes are local stand-ins for the deployed query API
 *, which speaks the canonical field names so an
 * integrator can grep the model and the payload with one word. Translating
 * here would put a rename between this app and the API it is meant to graduate to.
 *
 * Hand-written rather than generated, same as ui/: the producer is one Python
 * module, and a codegen step would be more machinery than contract. See
 * corridor_event_hub/core/trace.py for the shape and corridor_event_hub/trace_server.py for the routes.
 */

/** 'info' is a limit on a claim, not a defect. Only 'error' means something is wrong. */
export type Severity = 'info' | 'warn' | 'error';

export type StageId = 'ingest' | 'normalize' | 'resolve' | 'lifecycle' | 'terminal';

export type StageStatus = 'done' | 'current' | 'pending' | 'warning';

export interface Milepost {
  state: string;
  milepost: number;
}

/** One row in the record list. Enough to triage without opening the trace. */
export interface RecordRow {
  event_id: string;
  event_class: string;
  event_subtype: string;
  lifecycle_state: string;
  version: number;
  confidence: number;
  severity: string;
  direction: string;
  states: string[];
  begin_measure: number | null;
  end_measure: number | null;
  milepost_begin: Milepost | null;
  milepost_end: Milepost | null;
  conflation_method: string;
  agencies: string[];
  source_ids: string[];
  native_ids: string[];
  /** Agencies sharing an independence group corroborate ONCE. */
  independent_source_count: number;
  created_at: string;
  updated_at: string;
  start_time: string;
  end_time: string | null;
  last_source_update_at: string | null;
  ttl_expires_at: string | null;
  /** Briefly a race with the timer tick; persistently a dead timer chain. */
  ttl_expired: boolean;
  seconds_until_ttl: number | null;
  age_seconds: number | null;
  quiet_seconds: number | null;
  related_event_ids: string[];
  /** No corridor measure, so no corridor query can return it. */
  unresolved_extent: boolean;
  terminal: boolean;
  /** Only present on a related-event stub whose event could not be read. */
  missing?: boolean;
}

export interface RecordsResponse {
  generated_at: string;
  route: string;
  query: {
    lifecycle_state: string[];
    event_class: string[] | null;
    source_id: string[] | null;
    q: string | null;
    limit: number;
  };
  count: number;
  fetched_count: number;
  fetched_counts_by_state: Record<string, number>;
  truncated: boolean;
  truncation_note: string | null;
  records: RecordRow[];
  cache: CacheMeta;
}

export interface CacheMeta {
  served_from_cache: boolean;
  age_seconds: number;
  ttl_seconds: number;
}

/** One line of a version diff. */
export interface Change {
  path: string;
  from: unknown;
  to: unknown;
  /** True when this is a re-fetch artefact rather than news: raw_ref, retrieved_at, confidence. */
  bookkeeping: boolean;
  notable: boolean;
}

/** One recorded step in the record's life: an audit record plus the version it wrote. */
export interface Step {
  sequence: number;
  stage: StageId;
  from_state: string | null;
  to_state: string;
  trigger: string;
  actor: string;
  reason: string;
  rule_version: string;
  /** Two clocks. `occurred_at` is when the world changed, `recorded_at` when we wrote it. */
  occurred_at: string;
  recorded_at: string;
  lag_seconds: number | null;
  operator_id: string | null;
  /** The exact bytes that caused this step - the link back to ingestion. */
  payload_ref: string | null;
  /** False for a confirmation: a re-report at the same state is not a transition. */
  transition: boolean;
  legal: boolean;
  version: number | null;
  confidence: number | null;
  version_missing: boolean;
  changes: Change[];
  /** Nothing changed but re-fetch bookkeeping. Collapsible, and the basis of `version_churn`. */
  confirmation_only: boolean;
  diff_unavailable: boolean;
}

export interface StateSpan {
  state: string;
  entered_at: string;
  exited_at: string | null;
  first_step: number | null;
  last_step: number | null;
  /** Confirmations collapsed into this span. "40 re-reports" is itself a trust signal. */
  updates: number;
  trigger_in: string | null;
  seconds: number | null;
  current: boolean;
  /** The `current` pointer says this state; no audit record explains it. */
  unaudited?: boolean;
}

export interface Stage {
  stage: StageId;
  label: string;
  what: string;
  status: StageStatus;
  at: string | null;
  detail: string[];
  evidence: Record<string, unknown>;
}

export interface Finding {
  code: string;
  severity: Severity;
  detail: string;
}

export interface SourceRef {
  source_id: string;
  agency: string;
  native_id: string;
  retrieved_at: string;
  source_updated_at: string | null;
  contributed_fields: string[];
  raw_ref: string;
}

/**
 * How much of a long history was read. A record polled every 60 seconds for a day
 * has over 1,500 versions, so the API reads the tail - and says so here, because a
 * view showing 200 of 1,564 steps without saying which is the failure mode this
 * whole tool exists to prevent.
 */
export interface Window {
  windowed: boolean;
  steps_shown: number;
  steps_total: number;
  first_sequence_shown: number | null;
  note: string | null;
}

export interface Trace {
  event_id: string;
  generated_at: string;
  summary: RecordRow;
  current: Record<string, unknown>;
  stages: Stage[];
  steps: Step[];
  states: StateSpan[];
  findings: Finding[];
  window: Window;
  sources: SourceRef[];
  /** Which source won each field. */
  field_provenance: Record<string, unknown>;
  /** What the losing sources said, retained rather than discarded. */
  alternates: Record<string, unknown>;
  extensions: Record<string, unknown>;
  counts: {
    versions: number;
    audit: number;
    versions_read: number;
    audit_read: number;
    sources: number;
    payload_refs: number;
  };
  related: RecordRow[];
  cloud: CloudBlock;
  cache?: CacheMeta;
  /** Set instead of everything else when the id is not in the store. */
  error?: string;
}

/**
 * Which account is being read, and how it was found. Prominent in the UI on
 * purpose: every number on the screen comes from one specific deployed stack, and
 * "which account am I looking at" must never be something a reader infers.
 */
export interface CloudBlock {
  account: string | null;
  region: string;
  profile: string | null;
  caller_arn: string | null;
  event_table: string;
  source_catalog_table: string | null;
  raw_bucket: string | null;
  stack: string;
  discovered_via: string;
  query_api_url: string | null;
  dashboard_url: string | null;
  lifecycle_state_machine_arn: string | null;
  scheduled_sources: string[];
}

export interface Transition {
  from_state: string;
  to_state: string;
  triggers: string[];
  rationale: string;
}

/**
 * Reference data, SERVED rather than duplicated here. The transition table, the TTL
 * ladder and the class profiles all live in core/lifecycle.py; a copy in TypeScript
 * would drift the first time an edge changed and would do it silently.
 */
export interface Meta {
  generated_at: string;
  cloud: CloudBlock;
  route: string;
  lifecycle_states: string[];
  event_classes: string[];
  stages: Array<{ stage: StageId; label: string; what: string }>;
  trigger_stage: Record<string, StageId>;
  ttl_ladder: Record<string, string>;
  transitions: Transition[];
  lifecycle_profiles: Record<
    string,
    {
      ttl_seconds: Record<string, number>;
      reopen_window_seconds: number;
      confidence_half_life_seconds: number;
    }
  >;
  independence_groups: Record<string, string>;
  confidence_model: { version: string; weights: Record<string, number> };
  match_model: { version: string; merge_threshold: number; review_threshold: number };
  resolver_policy_version: string;
}

export interface SourceStatus {
  sourceId?: string;
  agency?: string;
  endpoint?: string;
  lastStatus?: number | string | null;
  lastAttemptAt?: string | null;
  lastSuccessAt?: string | null;
  lastFailureAt?: string | null;
  lastError?: string | null;
  lastLatencyMs?: number | null;
  lastBytes?: number | null;
}

export interface DlqStatus {
  queue: string;
  url: string;
  depth: number | null;
  in_flight: number | null;
  error: string | null;
}

export interface TimerHealth {
  state_machine_arn: string | null;
  sampled?: number;
  counts?: Record<string, number | null>;
  counts_capped?: boolean;
  failed?: Array<{
    name: string | null;
    status: string;
    started_at: string | null;
    stopped_at: string | null;
  }>;
  note?: string;
  errors?: Record<string, string>;
}

export interface Pipeline {
  generated_at: string;
  cloud: CloudBlock;
  route: string;
  state_counts: Record<string, number>;
  sources: SourceStatus[];
  dlqs: DlqStatus[];
  timers: TimerHealth;
  cache: CacheMeta;
}

/** The raw agency bytes a record came from. */
export interface RawPayload {
  ref: string;
  bucket: string;
  key: string;
  /** How much was read to search for the record; larger than what is shown. */
  bytes_read: number;
  /** How much of it is in `body`. */
  bytes_returned: number;
  bytes_total: number | null;
  truncated: boolean;
  last_modified: string | null;
  body: string;
  /** The one record inside the payload, extracted by native id when possible. */
  record: unknown;
  record_note: string | null;
}

/** What the API returns when it cannot reach the deployed stack. Rendered verbatim. */
export interface ApiError {
  error: string;
  detail?: string;
  hint?: string;
}
