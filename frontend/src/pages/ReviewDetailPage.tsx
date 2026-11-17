import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import ReviewForm from "../components/ReviewForm";
import ScoreBreakdown from "../components/ScoreBreakdown";
import { RiskBadge, StatusBadge } from "../components/Badges";
import { ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { getRisk, listReviews } from "../lib/api";
import { formatDate, humanise } from "../lib/format";

export default function ReviewDetailPage() {
  const { reviewId = "" } = useParams();
  const navigate = useNavigate();

  const queue = useQuery({ queryKey: ["reviews", "pending"], queryFn: () => listReviews("pending") });
  const review = queue.data?.reviews.find((row) => row.id === reviewId);
  const risk = useQuery({
    queryKey: ["risk", review?.audit_id],
    queryFn: () => getRisk(review!.audit_id),
    enabled: Boolean(review?.audit_id),
    retry: false,
  });

  if (queue.isPending) return <SkeletonRows rows={6} label="Loading the review" />;
  if (queue.isError) {
    return <ErrorState error={queue.error} onRetry={() => void queue.refetch()} />;
  }
  if (!review) {
    return (
      <ErrorState
        title="This review is no longer pending"
        error="It has already been decided, or it does not exist. The queue shows what still needs a reviewer."
      />
    );
  }

  return (
    <>
      <PageHeader
        title={review.repository ?? "Review"}
        subtitle={
          <span className="inline-badges">
            <RiskBadge value={review.level} />
            <StatusBadge value={review.status} />
            <span className="muted">requested {formatDate(review.requested_at)}</span>
          </span>
        }
        actions={
          <>
            <Link className="button" to={`/audits/${review.audit_id}/findings`}>
              Findings
            </Link>
            <Link className="button" to={`/audits/${review.audit_id}/report`}>
              Report
            </Link>
            <Link className="button" to="/reviews">
              Back to queue
            </Link>
          </>
        }
      />

      <div className="detail-grid">
        <div className="detail-main">
          <section className="panel">
            <h2>Assessment</h2>
            {risk.isPending ? <SkeletonRows rows={3} /> : null}
            {risk.isError ? (
              <p className="muted">The assessment could not be loaded for this audit.</p>
            ) : null}
            {risk.isSuccess ? (
              <>
                <p>
                  <strong>{humanise(risk.data.level)}</strong> by rule{" "}
                  <code>{risk.data.rule ?? "—"}</code>
                  {risk.data.score !== null ? ` · score ${risk.data.score.toFixed(2)}` : ""}
                </p>
                <p>{risk.data.rationale}</p>
                <p className="muted">
                  Counted: {Object.entries(risk.data.counts).map(([k, v]) => `${k} ${v}`).join(" · ")}
                  {" · "}rejected {risk.data.rejected}
                </p>
              </>
            ) : null}
          </section>

          <section className="panel">
            <h2>Scores behind the level</h2>
            {risk.isSuccess ? <ScoreBreakdown factors={risk.data.factors} /> : null}
          </section>
        </div>

        <aside className="detail-side">
          <ReviewForm
            review={review}
            onDecided={() => {
              void queue.refetch();
              navigate(`/audits/${review.audit_id}/progress`);
            }}
          />
        </aside>
      </div>
    </>
  );
}
