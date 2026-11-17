import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import DataTable, { type Column } from "../components/DataTable";
import { RiskBadge, StatusBadge } from "../components/Badges";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { useAuth } from "../context/AuthContext";
import { listAudits, type AuditRow } from "../lib/api";
import { formatDate, money, relativeTime, stageLabel } from "../lib/format";

const ACTIVE = new Set(["queued", "running"]);

function withinWeek(value: string | null): boolean {
  if (!value) return false;
  return Date.now() - new Date(value).getTime() <= 7 * 86_400_000;
}

export default function DashboardPage() {
  const { can, user } = useAuth();
  const query = useQuery({ queryKey: ["audits"], queryFn: () => listAudits(100) });

  const audits = query.data?.audits ?? [];
  const running = audits.filter((audit) => ACTIVE.has(audit.status)).length;
  const awaiting = audits.filter((audit) => audit.status === "awaiting_review").length;
  const completedWeek = audits.filter(
    (audit) => audit.status === "completed" && withinWeek(audit.finished_at)
  ).length;
  const cost = audits.reduce((total, audit) => total + audit.cost_usd, 0);

  const columns: Column<AuditRow>[] = [
    {
      key: "repository",
      header: "Repository",
      render: (row) => (
        <Link to={`/audits/${row.id}/progress`} className="link">
          {row.repository_name}
        </Link>
      ),
      sortValue: (row) => row.repository_name,
    },
    {
      key: "status",
      header: "Status",
      render: (row) => (
        <span className="stack">
          <StatusBadge value={row.status} />
          {row.current_stage ? <span className="muted">{stageLabel(row.current_stage)}</span> : null}
        </span>
      ),
      sortValue: (row) => row.status,
    },
    {
      key: "risk",
      header: "Overall risk",
      render: (row) => <RiskBadge value={row.risk_level} />,
      sortValue: (row) => row.risk_level ?? "",
    },
    {
      key: "cost",
      header: "Cost",
      render: (row) => <span className="mono">{money(row.cost_usd)}</span>,
      sortValue: (row) => row.cost_usd,
      align: "right",
    },
    {
      key: "date",
      header: "Started",
      render: (row) => (
        <span title={formatDate(row.started_at ?? row.created_at)}>
          {relativeTime(row.started_at ?? row.created_at)}
        </span>
      ),
      sortValue: (row) => row.created_at,
    },
  ];

  return (
    <>
      <PageHeader
        title={`Welcome${user ? `, ${user.email.split("@")[0]}` : ""}`}
        subtitle="Every audit in one place, newest first."
        actions={
          can("analyst") ? (
            <Link className="button primary" to="/repositories">
              New audit
            </Link>
          ) : undefined
        }
      />

      <section className="cards" aria-label="Audit summary">
        <div className="card stat">
          <span className="stat-label">Audits running</span>
          <span className="stat-value">{running}</span>
        </div>
        <div className="card stat">
          <span className="stat-label">Awaiting review</span>
          <span className="stat-value">{awaiting}</span>
        </div>
        <div className="card stat">
          <span className="stat-label">Completed this week</span>
          <span className="stat-value">{completedWeek}</span>
        </div>
        <div className="card stat">
          <span className="stat-label">Total cost</span>
          <span className="stat-value">{money(cost)}</span>
        </div>
      </section>

      <section className="panel">
        <h2>Recent audits</h2>
        {query.isPending ? <SkeletonRows rows={6} label="Loading audits" /> : null}
        {query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : null}
        {query.isSuccess ? (
          audits.length === 0 ? (
            <EmptyState
              title="No audits yet"
              action={
                can("analyst") ? (
                  <Link className="button primary" to="/repositories">
                    Upload a repository
                  </Link>
                ) : undefined
              }
            >
              Upload a repository and start an audit; it will appear here with its live status.
            </EmptyState>
          ) : (
            <DataTable
              caption="Recent audits"
              columns={columns}
              rows={audits}
              rowKey={(row) => row.id}
              initialSort={{ key: "date", direction: "desc" }}
            />
          )
        ) : null}
      </section>
    </>
  );
}
