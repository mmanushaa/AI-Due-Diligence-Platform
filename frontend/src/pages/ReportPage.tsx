import { useEffect, useState } from "react";
import { getReportHtml } from "../lib/api";

export default function ReportPage({ auditId }: { auditId: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    const run = async () => {
      try {
        const text = await getReportHtml(auditId);
        if (!cancelled) setHtml(text);
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
  }, [auditId]);

  if (loading) return <div className="loading">Loading report…</div>;
  if (error) return <div className="error">{error}</div>;
  if (!html) return <div className="empty">No report yet.</div>;

  return (
    <div
      className="report"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
