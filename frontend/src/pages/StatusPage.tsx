import { useEffect, useState } from "react";
import { getAudit, type AuditOut } from "../lib/api";

export default function StatusPage({
  auditId,
  repositoryId,
  onAuditStarted,
}: {
  repositoryId?: string;
  auditId?: string | null;
  onAuditStarted?: (auditId: string) => void;
}) {
  const effectiveId = auditId && typeof auditId === "string" ? auditId : repositoryId ?? null;
  const [status, setStatus] = useState<AuditOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    const run = async () => {
      if (!(effectiveId && typeof effectiveId === "string")) {
        setLoading(false);
        return;
      }
      try {
        const data = await getAudit(effectiveId);
        if (!cancelled) {
          setStatus(data);
          if (data.status === "queued" || data.status === "running") {
            onAuditStarted?.(String(data.id));
          }
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    run();
    return () => {
      cancelled = true;
    };
  }, [effectiveId, onAuditStarted]);

  return (
    <div className="page">
      <header className="page-header">
        <h1>Audit status</h1>
      </header>

      {loading && <div className="loading">Loading…</div>}
      {error && <div className="error">{error}</div>}
      {status && (
        <pre className="json">{JSON.stringify(status, null, 2)}</pre>
      )}

      {!effectiveId && (
        <p className="muted">Upload a repository, then start an audit to see status here.</p>
      )}
    </div>
  );
}
