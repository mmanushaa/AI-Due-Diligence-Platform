import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import DataTable, { type Column } from "../components/DataTable";
import FilterRail from "../components/FilterRail";
import { SeverityBadge, StatusBadge } from "../components/Badges";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { listFindings, type FindingEntry, type FindingFilters } from "../lib/api";

export default function FindingsPage() {
  const { auditId = "" } = useParams();
  const [params, setParams] = useSearchParams();

  const filters: FindingFilters = {
    category: params.get("category") ?? undefined,
    severity: params.get("severity") ?? undefined,
    status: params.get("status") ?? undefined,
    file: params.get("file") ?? undefined,
  };

  const query = useQuery({
    queryKey: ["findings", auditId, filters],
    queryFn: () => listFindings(auditId, filters),
    enabled: Boolean(auditId),
  });

  const findings = query.data?.findings ?? [];
  const statuses: Record<string, number> = {};
  for (const finding of findings) {
    statuses[finding.status] = (statuses[finding.status] ?? 0) + 1;
  }

  function apply(next: FindingFilters) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(next)) {
      if (value) search.set(key, value);
    }
    setParams(search);
  }

  const columns: Column<FindingEntry>[] = [
    {
      key: "title",
      header: "Finding",
      render: (row) => (
        <Link className="link" to={`/audits/${auditId}/findings/${row.id}`}>
          {row.title}
        </Link>
      ),
      sortValue: (row) => row.title,
    },
    {
      key: "category",
      header: "Category",
      render: (row) => row.category,
      sortValue: (row) => row.category,
    },
    {
      key: "severity",
      header: "Severity",
      render: (row) => <SeverityBadge value={row.severity} />,
      sortValue: (row) =>
        ["critical", "high", "medium", "low", "info"].indexOf(row.severity),
    },
    {
      key: "confidence",
      header: "Confidence",
      render: (row) =>
        row.confidence === null || row.confidence === undefined
          ? "—"
          : row.confidence.toFixed(2),
      sortValue: (row) => row.confidence ?? 0,
      align: "right",
    },
    {
      key: "status",
      header: "Verification",
      render: (row) => <StatusBadge value={row.status} />,
      sortValue: (row) => row.status,
    },
    {
      key: "location",
      header: "File:line",
      render: (row) =>
        row.file_path ? (
          <code className="mono">
            {row.file_path}
            {row.line_start ? `:${row.line_start}` : ""}
          </code>
        ) : (
          <span className="muted">no location</span>
        ),
      sortValue: (row) => row.file_path ?? "",
    },
  ];

  return (
    <>
      <PageHeader
        title="Findings"
        subtitle="Everything the run recorded, grouped by what was checked. Open a finding for its code and evidence."
        actions={
          <>
            <Link className="button" to={`/audits/${auditId}/risk`}>
              Risk breakdown
            </Link>
            <Link className="button" to={`/audits/${auditId}/progress`}>
              Progress
            </Link>
          </>
        }
      />

      <div className="findings-layout">
        <FilterRail
          filters={filters}
          total={query.data?.summary.total ?? 0}
          counts={{
            categories: query.data?.summary.by_category ?? {},
            severities: query.data?.summary.by_severity ?? {},
            statuses,
          }}
          onChange={apply}
        />

        <section className="panel grow" aria-live="polite">
          <div className="panel-head">
            <h2>
              {query.data ? `${findings.length} of ${query.data.summary.total} findings` : "Findings"}
            </h2>
            {query.data && (query.data.summary.high_critical ?? 0) > 0 ? (
              <span className="muted">{query.data.summary.high_critical ?? 0} high or critical</span>
            ) : null}
          </div>

          {query.isPending ? <SkeletonRows rows={8} label="Loading findings" /> : null}
          {query.isError ? (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          ) : null}
          {query.isSuccess ? (
            findings.length === 0 ? (
              <EmptyState
                title={Object.keys(filters).length ? "No findings match these filters" : "No findings recorded"}
                action={
                  Object.keys(filters).length ? (
                    <button type="button" className="button" onClick={() => apply({})}>
                      Clear filters
                    </button>
                  ) : undefined
                }
              >
                {Object.keys(filters).length
                  ? "Broaden the filters to see the rest of the audit."
                  : "The run recorded nothing for this audit. Limitations in the report say what was checked."}
              </EmptyState>
            ) : (
              <DataTable
                caption="Findings"
                columns={columns}
                rows={findings}
                rowKey={(row) => row.id}
                initialSort={{ key: "severity", direction: "asc" }}
              />
            )
          ) : null}
        </section>
      </div>
    </>
  );
}
