import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import ScoreBreakdown from "../components/ScoreBreakdown";
import { SeverityBadge } from "../components/Badges";
import { EmptyState, PageHeader, SkeletonRows } from "../components/States";
import { getRisk } from "../lib/api";
import { humanise } from "../lib/format";

export default function RiskPage() {
  const { auditId = "" } = useParams();
  const query = useQuery({
    queryKey: ["risk", auditId],
    queryFn: () => getRisk(auditId),
    enabled: Boolean(auditId),
    retry: false,
  });

  if (query.isPending) return <SkeletonRows rows={6} label="Loading the risk assessment" />;
  if (query.isError) {
    return (
      <>
        <PageHeader title="Risk breakdown" />
        <EmptyState
          title="No assessment yet"
          action={
            <Link className="button" to={`/audits/${auditId}/progress`}>
              Back to progress
            </Link>
          }
        >
          Risk scoring runs after validation. If the run is still in progress or failed earlier,
          there is nothing to show here.
        </EmptyState>
      </>
    );
  }

  const risk = query.data;
  return (
    <>
      <PageHeader
        title="Risk breakdown"
        subtitle="Every number here is computed in code from the stored findings; the factors are shown, not summarised."
        actions={
          <>
            <Link className="button" to={`/audits/${auditId}/findings`}>
              Findings
            </Link>
            <Link className="button" to={`/audits/${auditId}/report`}>
              Report
            </Link>
          </>
        }
      />

      <section className="cards">
        <div className={`card stat risk-${risk.level}`}>
          <span className="stat-label">Overall level</span>
          <span className="stat-value">
            <SeverityBadge value={risk.level} />
          </span>
          <span className="muted">
            {risk.floor ? `Severity floor: ${humanise(risk.floor)}` : null}
          </span>
        </div>
        <div className="card stat">
          <span className="stat-label">Rule applied</span>
          <span className="stat-value small mono">{risk.rule ?? "—"}</span>
          <span className="muted">
            {risk.escalations.length ? `Escalated by ${risk.escalations.join(", ")}` : "No escalation fired"}
          </span>
        </div>
        <div className="card stat">
          <span className="stat-label">Aggregate score (sum)</span>
          <span className="stat-value small mono">{risk.score?.toFixed(2) ?? "—"}</span>
          <span className="muted">Never an average</span>
        </div>
        <div className="card stat">
          <span className="stat-label">Assessment state</span>
          <span className="stat-value small">{humanise(risk.status)}</span>
          {risk.supersedes_id ? (
            <span className="muted mono small">supersedes {risk.supersedes_id.slice(0, 8)}</span>
          ) : null}
        </div>
      </section>

      <section className="panel">
        <h2>Why this level</h2>
        <p>{risk.rationale ?? "No rationale was stored for this assessment."}</p>
        <p className="muted">
          Findings counted:{" "}
          {Object.entries(risk.counts)
            .map(([severity, count]) => `${severity} ${count}`)
            .join(" · ") || "none"}{" "}
          · rejected {risk.rejected}
        </p>
      </section>

      <section className="panel">
        <h2>Factors per finding</h2>
        <ScoreBreakdown factors={risk.factors} />
      </section>

      <section className="cards">
        <div className="card">
          <h2>Technical severity</h2>
          <p>
            The level above: derived from the stored findings, their evidence and coverage. It is
            reproducible by hand from the factor table.
          </p>
        </div>
        <div className="card">
          <h2>Business risk</h2>
          <p>
            {risk.business_risk_note ??
              "Needs information outside the repository — revenue exposure, customer contracts, deployment context. Not folded into the technical score."}
          </p>
        </div>
      </section>
    </>
  );
}
