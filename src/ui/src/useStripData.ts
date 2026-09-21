/**
 * Fetches the strip document from the dev API, with polling and honest freshness.
 *
 * WHY FRESHNESS IS FIRST-CLASS HERE: the static viewer this replaces could show an
 * 18-hour-old snapshot that looked identical to one from 30 seconds ago. "Static
 * data that looks live" is the exact failure mode this UI must not have, so
 * the age of the data is part of the state this hook returns, not a detail.
 *
 * Polling defaults to 30s against a server that will not re-run the adapters more
 * often than every 20s (AZ511 allows 10 requests/60s). Going faster would just
 * return the same cached bytes while burning a request.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { StripData } from './types';

const DEFAULT_POLL_MS = 30_000;

export interface StripState {
  data: StripData | null;
  error: string | null;
  loading: boolean;
  /** Seconds since the document was generated, ticking between polls. */
  ageSeconds: number | null;
  lastFetchedAt: number | null;
  refresh: () => void;
  polling: boolean;
  setPolling: (on: boolean) => void;
}

export function useStripData(options?: { pollMs?: number }): StripState {
  const pollMs = options?.pollMs ?? DEFAULT_POLL_MS;

  const [data, setData] = useState<StripData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [polling, setPolling] = useState(true);
  const [lastFetchedAt, setLastFetchedAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  // Guards against a slow response from a previous request overwriting a newer
  // one - the classic out-of-order fetch bug, which here would silently show
  // older data than what has already been rendered.
  const requestSeq = useRef(0);

  const load = useCallback(
    async (force: boolean) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      try {
        const res = await fetch(`/api/strip${force ? '?refresh=1' : ''}`);
        if (!res.ok) {
          throw new Error(`API returned HTTP ${res.status}`);
        }
        const json = (await res.json()) as StripData;
        if (seq !== requestSeq.current) return; // superseded
        setData(json);
        setError(null);
        setLastFetchedAt(Date.now());
      } catch (e) {
        if (seq !== requestSeq.current) return;
        setError(
          e instanceof Error
            ? `${e.message}. Is the API running? Try \`npm run serve\` in src/.`
            : String(e),
        );
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  useEffect(() => {
    if (!polling) return;
    const id = setInterval(() => void load(false), pollMs);
    return () => clearInterval(id);
  }, [polling, pollMs, load]);

  // A separate 1s tick so the displayed age counts up continuously rather than
  // jumping only when a poll lands. Without this, a 30s-stale document reads as
  // "0s ago" for 29 of those seconds.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const ageSeconds = data ? Math.max(0, (now - Date.parse(data.generatedAt)) / 1000) : null;

  return {
    data,
    error,
    loading,
    ageSeconds,
    lastFetchedAt,
    refresh: () => void load(true),
    polling,
    setPolling,
  };
}
