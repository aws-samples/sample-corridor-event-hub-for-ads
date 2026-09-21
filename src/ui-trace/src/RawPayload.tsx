/**
 * The exact agency bytes a step came from.
 *
 * WHY THIS PANEL EXISTS: without it, the trace is the pipeline's account of what an
 * agency said. With it, you can read what the agency actually said. That is the
 * difference between a tool that explains the pipeline and one that can be used to
 * find the pipeline wrong.
 *
 * The extracted record is shown FIRST and the payload slice second: a 4 MB
 * WeatherShare fetch holds thousands of records and exactly one of them is this
 * event's. When extraction fails - a protobuf traffic tile, a truncated read, an id
 * the agency reissued - the panel says which, because "no record found" and "we did
 * not look properly" are different facts.
 */

import { humanBytes } from './derive';
import type { RawPayload as RawPayloadDoc } from './types';

export function RawPayload({
  payload,
  error,
  loading,
  onClose,
}: {
  payload: RawPayloadDoc | null;
  error: string | null;
  loading: boolean;
  onClose: () => void;
}) {
  if (!loading && !error && !payload) return null;

  return (
    <div className="drawer">
      <div className="drawer-head">
        <h2>Raw payload from the agency</h2>
        <span className="spacer" />
        <button onClick={onClose}>close</button>
      </div>

      {loading && <p className="sub">Reading from the raw zone&hellip;</p>}
      {error && (
        <div className="banner">
          <b>Could not read the payload.</b> {error}
        </div>
      )}

      {payload && (
        <>
          <p className="sub mono">
            {payload.key}
            <br />
            {humanBytes(payload.bytes_read)} read
            {payload.bytes_total !== null && ` of ${humanBytes(payload.bytes_total)}`}
            {payload.bytes_returned < payload.bytes_read &&
              `, ${humanBytes(payload.bytes_returned)} shown`}
            {payload.last_modified && ` - stored ${payload.last_modified}`}
          </p>

          {payload.record ? (
            <>
              <h3>This record, as the agency published it</h3>
              <pre className="raw">{JSON.stringify(payload.record, null, 2)}</pre>
            </>
          ) : (
            payload.record_note && <p className="hint">{payload.record_note}</p>
          )}

          <h3>The payload as fetched{payload.truncated && ', first slice'}</h3>
          <pre className="raw dim-pre">{payload.body}</pre>
        </>
      )}
    </div>
  );
}
