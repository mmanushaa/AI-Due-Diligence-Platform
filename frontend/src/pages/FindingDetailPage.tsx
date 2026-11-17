import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import CodeViewer from "../components/CodeViewer";
import { SeverityBadge, StatusBadge, VerdictBadge } from "../components/Badges";
import { ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { getFinding, getFindingCode, getFindingHistory } from "../lib/api";
import { formatDate, humanise } from "../lib/format";

export default function FindingDetailPage() {
  const { auditId = "", findingId = "" } = useParams();

  const finding = useQuery({
    queryKey: ["finding", findingId],
    queryFn: () => getFinding(findingId),
    enabled: Boolean(findingId),
  });
  const code = useQuery({
    queryKey: ["finding-code", findingId],
    queryFn: () => getFindingCode(findingId),
    enabled: Boolean(findingId),
    retry: false,
  });
  const history = useQuery({
    queryKey: ["finding-history", findingId],
    queryFn: () => getFindingHistory(findingId),
    enabled: Boolean(findingId),
  });

  if (finding.isPending) return <SkeletonRows rows={8} label="Loading the finding" />;
  if (finding.isError) {
    return <ErrorState error={finding.error} onRetry={() => void finding.refetch()} />;
  }

  const data = finding.data;
  const evidence = data.evidence ?? [];
  return (
    <>
      <PageHeader
        title={data.title}
        subtitle={
          <span className="inline-badges">
            <SeverityBadge value={data.severity} />
            <StatusBadge value={data.status} />
            <VerdictBadge value={data.verdict} />
            <span className="muted">{humanise(data.category)}</span>
          </span>
        }
        actions={
          <>
            <Link className="button" to={`/audits/${auditId}/findings`}>
              Back to findings
            </Link>
            <Link className="button" to={`/audits/${auditId}/risk`}>
              Risk breakdown
            </Link>
          </>
        }
      />

      <div className="detail-grid">
        <div className="detail-main">
          {code.isPending ? <SkeletonRows rows={6} label="Loading code" /> : null}
          {code.isError ? (
            <div className="panel">
              <h2>Code</h2>
              <p className="muted">
                {code.error instanceof Error ? code.error.message : "The code could not be loaded."}
              </p>
            </div>
          ) : null}
          {code.isSuccess ? <CodeViewer code={code.data} /> : null}

          <section className="panel">
            <h2>Evidence</h2>
            {evidence.length === 0 ? (
              <p className="muted">No evidence rows were stored for this finding.</p>
            ) : (
              <ul className="evidence-list">
                {evidence.map((row) => (
                  <li key={row.id}>
                    <div className="evidence-head">
                      <code className="mono">
                        {row.file_path}:{row.line_start}–{row.line_end}
                      </code>
                      <span className="muted">{row.kind}</span>
                    </div>
                    {row.snippet ? <pre className="snippet">{row.snippet}</pre> : null}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="panel">
            <h2>Status history</h2>
            {history.isPending ? <SkeletonRows rows={2} /> : null}
            {history.isSuccess ? (
              history.data.events.length === 0 ? (
                <p className="muted">No status change was recorded for this finding.</p>
              ) : (
                <ol className="history">
                  {history.data.events.map((event) => (
                    <li key={event.id}>
                      <span className="mono muted">{formatDate(event.created_at)}</span>{" "}
                      {event.from_status ? `${humanise(event.from_status)} → ` : ""}
                      <strong>{humanise(event.to_status)}</strong>{" "}
                      <span className="muted">by {event.actor}</span>
                      {event.note ? <p className="muted">{event.note}</p> : null}
                    </li>
                  ))}
                </ol>
              )
            ) : null}
          </section>
        </div>

        <aside className="detail-side" aria-label="Finding context">
          <section className="panel">
            <h2>Assessment</h2>
            <dl className="facts">
              <dt>Confidence</dt>
              <dd>{data.confidence?.toFixed(2) ?? "—"}</dd>
              <dt>Source</dt>
              <dd>{data.source_tool ?? "—"}</dd>
              <dt>Location</dt>
              <dd className="mono">
                {data.file_path ? `${data.file_path}:${data.line_start ?? ""}` : "—"}
              </dd>
              <dt>Fingerprint</dt>
              <dd className="mono small">{data.fingerprint}</dd>
            </dl>
          </section>

          <section className="panel">
            <h2>Reasoning</h2>
            <p>{data.description ?? "No reasoning was recorded."}</p>
          </section>

          <section className="panel">
            <h2>Business impact</h2>
            <p>{data.business_impact ?? "Not assessed — business impact needs information outside the repository."}</p>
          </section>

          <section className="panel">
            <h2>Recommendation</h2>
            <p>{data.recommendation ?? "No recommendation was recorded."}</p>
          </section>

          <section className="panel">
            <h2>Related files</h2>
            <p className="muted">
              {evidence.length
                ? "The evidence rows above are the context retrieved for this finding; no separate file list is stored."
                : "The agent retrieved no additional files for this finding."}
            </p>
          </section>
        </aside>
      </div>
    </>
  );
}
