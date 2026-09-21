/**
 * The pipeline around the records: is anything arriving, and is anything stuck.
 *
 * ON THE SAME TOOL AS THE RECORDS BECAUSE AN EMPTY LIST IS AMBIGUOUS. No records in
 * `active` means a quiet corridor if the feeds are being fetched and the dead-letter
 * queues are empty, and means a broken pipeline if they are not. Those are opposite
 * conclusions from the same screen, and only this panel separates them.
 */

import { clockTime, humanBytes, humanDuration, secondsSince } from './derive';
import type { Pipeline as PipelineDoc } from './types';

export function Pipeline({ doc, now }: { doc: PipelineDoc; now: number }) {
  const stuck = doc.dlqs.filter((q) => (q.depth ?? 0) > 0);
  const failedTimers = doc.timers.counts?.FAILED ?? 0;
  const totalRecords = Object.values(doc.state_counts).reduce((sum, n) => sum + n, 0);

  return (
    <div className="trace">
      <div className="card">
        <h2>Records in the store, by lifecycle state</h2>
        <div className="counts">
          {Object.entries(doc.state_counts).map(([state, count]) => (
            <div key={state} className="count">
              <span className={`state s-${state}`}>{state}</span>
              <b>{count.toLocaleString()}</b>
            </div>
          ))}
          <div className="count total">
            <span className="dim">current records</span>
            <b>{totalRecords.toLocaleString()}</b>
          </div>
        </div>
        <p className="hint">
          Counted from the corridor index, so a record whose extent never conflated is not in these
          numbers &mdash; it is stored and readable by id, but no corridor query can return it
         . Records in <span className="mono">cleared</span> and{' '}
          <span className="mono">merged</span> are history, not live.
        </p>
      </div>

      <div className="card">
        <h2>Ingestion &mdash; the collector's own record of every fetch</h2>
        {doc.sources.length === 0 ? (
          <p className="empty">
            No source catalog table found in this account, so per-feed fetch status is unavailable.
          </p>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>source</th>
                <th>last attempt</th>
                <th>status</th>
                <th>latency</th>
                <th>payload</th>
                <th>last error</th>
              </tr>
            </thead>
            <tbody>
              {doc.sources.map((source) => {
                const age = secondsSince(source.lastSuccessAt ?? null, now);
                const failing = Boolean(source.lastError) || String(source.lastStatus) !== '200';
                return (
                  <tr key={source.sourceId} className={failing ? 'bad-row' : undefined}>
                    <td className="mono">{source.sourceId}</td>
                    <td className="mono">
                      {clockTime(source.lastAttemptAt ?? null)}
                      {age !== null && <span className="dim"> ({humanDuration(age)} ago)</span>}
                    </td>
                    <td className="mono">{source.lastStatus ?? '-'}</td>
                    <td className="mono">
                      {source.lastLatencyMs !== null && source.lastLatencyMs !== undefined
                        ? `${source.lastLatencyMs} ms`
                        : '-'}
                    </td>
                    <td className="mono">{humanBytes(source.lastBytes ?? null)}</td>
                    <td className="mono">{source.lastError ?? '-'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h2>Dead-letter queues &mdash; should be empty</h2>
        {stuck.length > 0 && (
          <div className="banner">
            <b>{stuck.length} queue(s) hold messages.</b> Records in a dead-letter queue are records
            that never reached the store, so they are absent from the list on the other tab rather
            than visible as failures. <span className="mono">npm run dlq-peek</span> shows what is in
            them without consuming anything.
          </div>
        )}
        <table className="tbl">
          <thead>
            <tr>
              <th>queue</th>
              <th>depth</th>
              <th>in flight</th>
            </tr>
          </thead>
          <tbody>
            {doc.dlqs.map((queue) => (
              <tr key={queue.queue} className={(queue.depth ?? 0) > 0 ? 'bad-row' : undefined}>
                <td className="mono">{queue.queue}</td>
                <td className="mono">{queue.error ? `error: ${queue.error}` : queue.depth}</td>
                <td className="mono">{queue.in_flight ?? '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>TTL timers</h2>
        {doc.timers.state_machine_arn === null ? (
          <p className="empty">No lifecycle state machine found in this account.</p>
        ) : (
          <>
            {failedTimers > 0 && (
              <div className="banner">
                <b>{failedTimers} failed timer execution(s).</b> Each one is an event that will never
                expire on its own &mdash; it will sit published past its TTL until a source update
                moves it. Those records show a <span className="mono">ttl_expired_not_moved</span>{' '}
                finding on the records tab.
              </div>
            )}
            <div className="counts">
              {Object.entries(doc.timers.counts ?? {}).map(([status, count]) => (
                <div key={status} className="count">
                  <span className="dim">{status.toLowerCase()}</span>
                  <b>{count === null ? '?' : count.toLocaleString()}</b>
                </div>
              ))}
            </div>
            <p className="hint">{doc.timers.note}</p>
            {(doc.timers.failed ?? []).length > 0 && (
              <table className="tbl">
                <thead>
                  <tr>
                    <th>execution (the event id)</th>
                    <th>status</th>
                    <th>started</th>
                    <th>stopped</th>
                  </tr>
                </thead>
                <tbody>
                  {(doc.timers.failed ?? []).map((execution) => (
                    <tr key={`${execution.name}-${execution.status}`} className="bad-row">
                      <td className="mono">{execution.name}</td>
                      <td className="mono">{execution.status}</td>
                      <td className="mono">{clockTime(execution.started_at)}</td>
                      <td className="mono">{clockTime(execution.stopped_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>

      <div className="card">
        <h2>What this tool is reading</h2>
        <dl className="evidence">
          <div>
            <dt className="mono">account</dt>
            <dd className="mono">
              {doc.cloud.account} ({doc.cloud.region})
            </dd>
          </div>
          <div>
            <dt className="mono">profile</dt>
            <dd className="mono">{doc.cloud.profile ?? '<default credentials>'}</dd>
          </div>
          <div>
            <dt className="mono">caller</dt>
            <dd className="mono">{doc.cloud.caller_arn}</dd>
          </div>
          <div>
            <dt className="mono">event store</dt>
            <dd className="mono">{doc.cloud.event_table}</dd>
          </div>
          <div>
            <dt className="mono">raw zone</dt>
            <dd className="mono">{doc.cloud.raw_bucket ?? '-'}</dd>
          </div>
          <div>
            <dt className="mono">discovered via</dt>
            <dd className="mono">
              {doc.cloud.discovered_via} ({doc.cloud.stack})
            </dd>
          </div>
          <div>
            <dt className="mono">deployed query API</dt>
            <dd className="mono">{doc.cloud.query_api_url ?? '-'}</dd>
          </div>
          <div>
            <dt className="mono">scheduled sources</dt>
            <dd className="mono">{doc.cloud.scheduled_sources.join(', ') || '-'}</dd>
          </div>
        </dl>
        <p className="hint">
          Every name above was discovered from the deployed CloudFormation outputs, never hardcoded
          &mdash; so this tool cannot quietly read the wrong account. All access is read-only: the
          API has no write path at all.
        </p>
      </div>
    </div>
  );
}
