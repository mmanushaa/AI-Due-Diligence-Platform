import { useState } from "react";
import StageTracker from "../components/StageTracker";
import { startAudit, type AuditOut, type ProgressSnapshot } from "../lib/api";

export default function StatusPage({
  repositoryId,
  onAuditStarted,
}: {
  repositoryId?: string;
  onAuditStarted?: (auditId: string) => void;
}) {
  const [audit, setAudit] = useState<AuditOut | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const active =
    audit !== null && (audit.status === "queued" || audit.status === "running");

  const handleStart = async () => {
    if (!repositoryId) return;
    setStarting(true);
    setError(null);
    try {
      const created = await startAudit(repositoryId);
      setAudit(created);
      onAuditStarted?.(String(created.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  };

  // The tracker is the source of truth once it is connected: the card below follows
  // the same events the stage list does, so there is no second polling path.
  const handleProgress = (snapshot: ProgressSnapshot) => {
    setAudit((current) =>
      current
        ? {
            ...current,
            status: snapshot.status,
            current_stage: snapshot.current_stage,
            finished_at: snapshot.final
              ? (current.finished_at ?? snapshot.at)
              : current.finished_at,
          }
        : current
    );
  };

  return (
    <div className="page">
      <header className="page-header">
        <h1>Audit status</h1>
        <p className="muted">
          Run the scanners against the uploaded snapshot and watch each stage.
        </p>
      </header>

      {!repositoryId && <p className="muted">Upload a repository first.</p>}

      {repositoryId && (
        <div className="field-actions">
          <button
            className="btn btn-primary"
            onClick={handleStart}
            disabled={starting || active}
          >
            {starting ? "Starting…" : active ? "Running…" : "Start audit"}
          </button>
        </div>
      )}

      {error && <div className="error">{error}</div>}

      {audit && (
        <dl className="status-grid">
          <dt>Audit</dt>
          <dd>{String(audit.id)}</dd>
          <dt>Status</dt>
          <dd>
            <span className={`status status-${audit.status}`}>{audit.status}</span>
          </dd>
          <dt>Stage</dt>
          <dd>{audit.current_stage ?? "—"}</dd>
          <dt>Started</dt>
          <dd>{audit.started_at ?? "—"}</dd>
          <dt>Finished</dt>
          <dd>{audit.finished_at ?? "—"}</dd>
          {audit.error && (
            <>
              <dt>Error</dt>
              <dd className="field-error">{audit.error}</dd>
            </>
          )}
        </dl>
      )}

      {audit && <StageTracker auditId={String(audit.id)} onProgress={handleProgress} />}

      {audit && !active && (
        <p className="muted">
          Findings are on the Findings tab ·{" "}
          <a href={`/api/v1/audits/${audit.id}/report`} target="_blank" rel="noreferrer">
            open the HTML report
          </a>
        </p>
      )}
    </div>
  );
}
