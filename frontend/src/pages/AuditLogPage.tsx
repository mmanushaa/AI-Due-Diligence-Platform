import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import DataTable, { type Column } from "../components/DataTable";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { getAuditLog, type AuditLogEntry } from "../lib/api";
import { formatDate, humanise, prettyJson } from "../lib/format";

export default function AuditLogPage() {
  const { auditId = "" } = useParams();
  const query = useQuery({
    queryKey: ["audit-log", auditId],
    queryFn: () => getAuditLog(auditId),
    enabled: Boolean(auditId),
  });

  const columns: Column<AuditLogEntry>[] = [
    {
      key: "time",
      header: "Time",
      render: (row) => <span className="mono small">{formatDate(row.created_at)}</span>,
      sortValue: (row) => row.created_at,
    },
    {
      key: "actor",
      header: "User",
      render: (row) => <span className="mono small">{row.actor}</span>,
      sortValue: (row) => row.actor,
    },
    {
      key: "action",
      header: "Action",
      render: (row) => humanise(row.action),
      sortValue: (row) => row.action,
    },
    {
      key: "object",
      header: "Object",
      render: (row) => (
        <span className="stack">
          <span>{humanise(row.object_type)}</span>
          <span className="mono muted small">{row.object_id}</span>
        </span>
      ),
      sortValue: (row) => row.object_type,
    },
    {
      key: "values",
      header: "Before / after",
      render: (row) =>
        row.before || row.after ? (
          <details>
            <summary>Values</summary>
            <div className="stack">
              <pre className="snippet">before: {prettyJson(row.before)}</pre>
              <pre className="snippet">after: {prettyJson(row.after)}</pre>
            </div>
          </details>
        ) : (
          <span className="muted">—</span>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle="Append-only record of this audit. Rows cannot be edited or deleted, and this screen cannot write."
        actions={
          <Link className="button" to={`/audits/${auditId}/progress`}>
            Back to audit
          </Link>
        }
      />

      {query.isPending ? <SkeletonRows rows={8} label="Loading the audit log" /> : null}
      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {query.isSuccess ? (
        query.data.entries.length === 0 ? (
          <EmptyState title="No entries yet">
            The trail fills as soon as the audit is created; nothing has been recorded for this id.
          </EmptyState>
        ) : (
          <DataTable
            caption="Audit trail"
            columns={columns}
            rows={query.data.entries}
            rowKey={(row) => row.id}
            initialSort={{ key: "time", direction: "asc" }}
          />
        )
      ) : null}
    </>
  );
}
