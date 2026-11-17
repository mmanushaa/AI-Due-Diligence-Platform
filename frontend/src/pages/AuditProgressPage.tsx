import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import StageTracker from "../components/StageTracker";
import EventLog, { type LogLine } from "../components/EventLog";
import { StatusBadge } from "../components/Badges";
import { ErrorState, IncompleteBanner, PageHeader, SkeletonRows } from "../components/States";
import {
  getAudit,
  progressEventsUrl,
  type ProgressSnapshot,
  type StageState,
} from "../lib/api";
import { money, stageLabel } from "../lib/format";

interface LiveState {
  status: string;
  currentStage: string | null;
  stages: Record<string, StageState>;
  costUsd: number;
  elapsedMs: number;
  final: boolean;
}

export default function AuditProgressPage() {
  const { auditId = "" } = useParams();
  const queryClient = useQueryClient();
  const audit = useQuery({
    queryKey: ["audit", auditId],
    queryFn: () => getAudit(auditId),
    enabled: Boolean(auditId),
  });
  const [live, setLive] = useState<LiveState | null>(null);
  const [lines, setLines] = useState<LogLine[]>([]);

  useEffect(() => {
    if (!auditId) return;
    const source = new EventSource(progressEventsUrl(auditId));
    source.onmessage = (event) => {
      const snapshot = JSON.parse(event.data) as ProgressSnapshot;
      setLive({
        status: snapshot.status,
        currentStage: snapshot.current_stage,
        stages: snapshot.stages ?? {},
        costUsd: snapshot.cost_usd,
        elapsedMs: snapshot.elapsed_ms,
        final: snapshot.final,
      });
      const text = `Stage ${stageLabel(snapshot.current_stage)} · ${snapshot.status}`;
      setLines((current) => {
        const last = current.at(-1);
        if (last?.text === text) return current;
        return [
          ...current,
          { id: `${snapshot.at}-${current.length}`, at: snapshot.at, text },
        ].slice(-200);
      });
      if (snapshot.final) {
        source.close();
        void queryClient.invalidateQueries({ queryKey: ["audit", auditId] });
        void queryClient.invalidateQueries({ queryKey: ["audits"] });
      }
    };
    source.onerror = () => {
      // The stream reconnects on its own; polling below keeps the header honest.
    };
    return () => source.close();
  }, [auditId, queryClient]);

  const view = useMemo(() => {
    const failed = audit.data?.status === "failed";
    return {
      status: live?.status ?? audit.data?.status ?? "unknown",
      currentStage: live?.currentStage ?? audit.data?.current_stage ?? null,
      stages: live?.stages,
      cost: live?.costUsd,
      elapsed: live?.elapsedMs,
      failed,
    };
  }, [audit.data, live]);

  if (audit.isPending) return <SkeletonRows rows={6} label="Loading the audit" />;
  if (audit.isError) {
    return <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />;
  }

  const finished = ["completed", "needs_evidence", "failed"].includes(view.status);

  return (
    <>
      <PageHeader
        title="Audit progress"
        subtitle={
          <span className="inline-badges">
            <StatusBadge value={view.status} />
            <span className="muted mono">audit {auditId}</span>
            {view.currentStage ? <span className="muted">{stageLabel(view.currentStage)}</span> : null}
          </span>
        }
        actions={
          <>
            <Link className="button" to={`/audits/${auditId}/findings`}>
              Findings
            </Link>
            <Link className="button" to={`/audits/${auditId}/risk`}>
              Risk
            </Link>
            <Link className="button" to={`/audits/${auditId}/report`}>
              Report
            </Link>
          </>
        }
      />

      <IncompleteBanner reason={audit.data?.error} />

      <section className="cards">
        <div className="card stat">
          <span className="stat-label">Current stage</span>
          <span className="stat-value small">{stageLabel(view.currentStage)}</span>
        </div>
        <div className="card stat">
          <span className="stat-label">Elapsed</span>
          <span className="stat-value small">
            {view.elapsed ? `${Math.round(view.elapsed / 1000)}s` : "—"}
          </span>
        </div>
        <div className="card stat">
          <span className="stat-label">Running cost</span>
          <span className="stat-value small">{money(view.cost ?? 0)}</span>
        </div>
        <div className="card stat">
          <span className="stat-label">Audit id</span>
          <span className="stat-value small mono">{auditId.slice(0, 8)}</span>
        </div>
      </section>

      <section className="panel">
        <h2>Stages</h2>
        <StageTracker stages={view.stages} currentStage={view.currentStage} />
        {view.status === "failed" ? (
          <p className="form-error" role="alert">
            This run failed. Findings produced before the failure are kept and labelled partial.
          </p>
        ) : null}
        {finished && view.status !== "failed" ? (
          <p className="form-ok" role="status">
            The run has finished. Read the findings, check the score, or open the report.
          </p>
        ) : null}
      </section>

      <EventLog lines={lines} />
    </>
  );
}
