/** Badges carry their label as text; colour is a second cue, never the only one. */

const SEVERITY_LABEL: Record<string, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
  info: "Info",
};

const STATUS_LABEL: Record<string, string> = {
  scanner_confirmed: "Scanner confirmed",
  ai_suggested: "AI suggested",
  verified: "Verified",
  rejected_false_positive: "Rejected",
  unresolved: "Unresolved",
  running: "Running",
  queued: "Queued",
  completed: "Completed",
  failed: "Failed",
  awaiting_review: "Awaiting review",
  needs_evidence: "Needs evidence",
  pending: "Pending",
  approved: "Approved",
  overridden: "Overridden",
  skipped: "Skipped",
  done: "Done",
};

const VERDICT_LABEL: Record<string, string> = {
  issue: "Issue",
  no_issue: "No issue",
  insufficient_evidence: "Insufficient evidence",
};

export function SeverityBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">—</span>;
  return (
    <span className={`badge severity severity-${value}`}>
      <span aria-hidden="true" className="badge-dot" />
      {SEVERITY_LABEL[value] ?? value}
    </span>
  );
}

export function StatusBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">—</span>;
  return (
    <span className={`badge status status-${value}`}>{STATUS_LABEL[value] ?? value.replaceAll("_", " ")}</span>
  );
}

export function VerdictBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">No verdict</span>;
  return (
    <span className={`badge verdict verdict-${value}`}>
      {VERDICT_LABEL[value] ?? value.replaceAll("_", " ")}
    </span>
  );
}

export function RiskBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">Not assessed</span>;
  return (
    <span className="badge risk">
      Risk: <SeverityBadge value={value} />
    </span>
  );
}

export function RoleBadge({ value }: { value: string }) {
  return <span className="badge role">{value}</span>;
}
