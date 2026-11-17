import type { StageState } from "../lib/api";

const STAGES: { key: string; label: string }[] = [
  { key: "intake", label: "Intake" },
  { key: "index", label: "Index" },
  { key: "scanners", label: "Scanners" },
  { key: "agents", label: "Agents" },
  { key: "validation", label: "Validation" },
  { key: "risk", label: "Risk" },
  { key: "review", label: "Review" },
  { key: "report", label: "Report" },
];

const TEXT: Record<StageState, string> = {
  pending: "Pending",
  running: "Running",
  done: "Done",
  failed: "Failed",
  skipped: "Skipped",
};

interface Props {
  stages: Record<string, StageState> | undefined;
  currentStage: string | null;
}

export default function StageTracker({ stages = {}, currentStage }: Props) {
  const done = STAGES.filter((stage) => stages[stage.key] === "done").length;
  const percent = Math.round((done / STAGES.length) * 100);

  return (
    <section className="tracker" aria-label="Audit stages">
      <div
        className="progress"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-label={`${done} of ${STAGES.length} stages complete`}
      >
        <div className="progress-fill" style={{ width: `${percent}%` }} />
      </div>
      <ol className="stage-list">
        {STAGES.map((stage) => {
          const state: StageState = stages[stage.key] ?? "pending";
          const isCurrent = currentStage === stage.key;
          return (
            <li key={stage.key} className={`stage stage-${state}${isCurrent ? " current" : ""}`}>
              <span className="stage-name">{stage.label}</span>
              <span className="stage-state">{TEXT[state]}</span>
              {state === "failed" ? (
                <span className="stage-note">Stage failed — partial results kept</span>
              ) : null}
            </li>
          );
        })}
      </ol>
      <p className="visually-hidden" aria-live="polite">
        {`Current stage: ${currentStage ?? "none"}. ${done} of ${STAGES.length} stages complete.`}
      </p>
    </section>
  );
}
