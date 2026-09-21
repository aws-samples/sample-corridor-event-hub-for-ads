/**
 * Fetching for the tracker: the record list, one record's trace, and pipeline health.
 *
 * TWO THINGS THIS DOES THAT MATTER MORE THAN THE FETCHING.
 *
 * It keeps the last good response on screen when a poll fails, and says so. The
 * alternative - blanking the view - loses the trace someone was reading because a
 * credential expired mid-session, which is precisely when they were reading it.
 *
 * It reports the API's own error text verbatim. The server distinguishes "cannot
 * reach the deployed stack, here is the profile and region it tried" from a 500, and
 * flattening both into "failed to fetch" sends someone debugging the browser when
 * the answer is an expired SSO session.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { Meta, Pipeline, RawPayload, RecordsResponse, Trace } from './types';

/**
 * Polling interval. The server will not re-read DynamoDB for a listing more often
 * than every 20s, so anything faster returns identical bytes and burns a request -
 * the same reasoning as the strip UI, for a different underlying limit (cost and
 * latency here, feed rate limits there).
 */
const DEFAULT_POLL_MS = 30_000;

async function request<T>(url: string): Promise<T> {
  const response = await fetch(url);
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    // A non-JSON body from a JSON API: report the status and the first of it rather
    // than a parse error, which describes this function instead of the problem.
    throw new Error(`HTTP ${response.status} with a non-JSON body: ${text.slice(0, 200)}`);
  }
  if (!response.ok) {
    const detail = payload as { error?: string; detail?: string; hint?: string } | null;
    throw new Error(
      detail?.detail || detail?.hint || detail?.error || `HTTP ${response.status}`,
    );
  }
  return payload as T;
}

interface Fetched<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: (force?: boolean) => void;
}

/**
 * One fetch with polling, superseded-response guarding, and last-good retention.
 *
 * The sequence guard is not theoretical here: a listing takes ~2s against the cloud
 * and a trace ~0.5s, so a slow list response landing after a fast one would show
 * older data than what is already rendered, with nothing on screen to indicate it.
 */
function useFetched<T>(url: string | null, pollMs: number | null): Fetched<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const sequence = useRef(0);
  const activeUrl = useRef<string | null>(null);

  const load = useCallback(
    async (force = false) => {
      if (!url) {
        setData(null);
        setError(null);
        return;
      }
      const mine = ++sequence.current;
      const target = force ? `${url}${url.includes('?') ? '&' : '?'}refresh=1` : url;
      const changedTarget = activeUrl.current !== url;
      activeUrl.current = url;
      // Clear stale data when the URL itself changed - a different record's trace
      // must never be shown under a new record's heading, even for one frame.
      if (changedTarget) setData(null);
      setLoading(true);
      try {
        const payload = await request<T>(target);
        if (mine !== sequence.current) return;
        setData(payload);
        setError(null);
      } catch (e) {
        if (mine !== sequence.current) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (mine === sequence.current) setLoading(false);
      }
    },
    [url],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  useEffect(() => {
    if (!pollMs || !url) return;
    const id = setInterval(() => void load(false), pollMs);
    return () => clearInterval(id);
  }, [pollMs, url, load]);

  return { data, error, loading, reload: load };
}

export function useMeta(): Fetched<Meta> {
  // No polling: the transition table and the account identity do not change while
  // the app is open, and re-reading them would add a describe-stacks per interval.
  return useFetched<Meta>('/api/meta', null);
}

export function useRecords(params: {
  states: string[];
  limit: number;
  polling: boolean;
}): Fetched<RecordsResponse> {
  const query = new URLSearchParams();
  if (params.states.length > 0) query.set('state', params.states.join(','));
  query.set('limit', String(params.limit));
  return useFetched<RecordsResponse>(
    `/api/records?${query.toString()}`,
    params.polling ? DEFAULT_POLL_MS : null,
  );
}

export function useTrace(eventId: string | null, polling: boolean): Fetched<Trace> {
  return useFetched<Trace>(
    eventId ? `/api/records/${encodeURIComponent(eventId)}` : null,
    // Faster than the listing: a trace is what someone watches while a record moves.
    polling ? 15_000 : null,
  );
}

export function usePipeline(polling: boolean): Fetched<Pipeline> {
  return useFetched<Pipeline>('/api/pipeline', polling ? DEFAULT_POLL_MS : null);
}

/**
 * The raw agency bytes for one payload reference, fetched on demand.
 *
 * On demand and never as part of a trace: a raw fetch is up to 512 KB and most
 * traces are read without anyone opening one. Loading them eagerly would make every
 * trace 50x heavier for a panel that is usually closed.
 */
export function useRawPayload(): {
  payload: RawPayload | null;
  error: string | null;
  loading: boolean;
  open: (ref: string, nativeId?: string | null) => void;
  close: () => void;
} {
  const [payload, setPayload] = useState<RawPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const sequence = useRef(0);

  const open = useCallback((ref: string, nativeId?: string | null) => {
    const mine = ++sequence.current;
    const query = new URLSearchParams({ ref });
    if (nativeId) query.set('native_id', nativeId);
    setLoading(true);
    setPayload(null);
    setError(null);
    void request<RawPayload>(`/api/raw?${query.toString()}`)
      .then((got) => {
        if (mine === sequence.current) setPayload(got);
      })
      .catch((e: unknown) => {
        if (mine === sequence.current) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (mine === sequence.current) setLoading(false);
      });
  }, []);

  const close = useCallback(() => {
    sequence.current += 1;
    setPayload(null);
    setError(null);
    setLoading(false);
  }, []);

  return { payload, error, loading, open, close };
}

/** A 1s tick, so an age counts up continuously instead of jumping on each poll. */
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}
