import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import DataTable, { type Column } from "../components/DataTable";
import { RiskBadge, StatusBadge } from "../components/Badges";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { listReviews, type ReviewOut } from "../lib/api";
import { relativeTime } from "../lib/format";

export default function ReviewQueuePage() {
  const query = useQuery({ queryKey: ["reviews", "pending"], queryFn: () => listReviews("pending") });

  const columns: Column<ReviewOut>[] = [
    {
      key: "repository",
      header: "Repository",
      render: (row) => (
        <Link className="link" to={`/reviews/${row.id}`}>
          {row.repository ?? "(deleted repository)"}
        </Link>
      ),
      sortValue: (row) => row.repository ?? "",
    },
    {
      key: "level",
      header: "Risk",
      render: (row) => <RiskBadge value={row.level} />,
      sortValue: (row) => row.level ?? "",
    },
    {
      key: "status",
      header: "Status",
      render: (row) => <StatusBadge value={row.status} />,
      sortValue: (row) => row.status,
    },
    {
      key: "age",
      header: "Waiting",
      render: (row) => (
        <span title={row.requested_at ?? undefined}>
          {relativeTime(row.requested_at ?? undefined)}
        </span>
      ),
      sortValue: (row) => row.requested_at ?? "",
    },
  ];

  return (
    <>
      <PageHeader
        title="Review queue"
        subtitle="Critical audits stop here until a reviewer approves, overrides with a reason, or asks for evidence."
      />
      {query.isPending ? <SkeletonRows rows={5} label="Loading the review queue" /> : null}
      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {query.isSuccess ? (
        query.data.reviews.length === 0 ? (
          <EmptyState title="Nothing awaiting review">
            A critical audit appears here when its run reaches the review gate.
          </EmptyState>
        ) : (
          <DataTable
            caption="Reviews awaiting a decision"
            columns={columns}
            rows={query.data.reviews}
            rowKey={(row) => row.id}
            initialSort={{ key: "age", direction: "asc" }}
          />
        )
      ) : null}
    </>
  );
}
