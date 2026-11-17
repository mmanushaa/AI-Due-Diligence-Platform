import { useState, type FormEvent } from "react";
import { decideReview, type DecisionOut, type ReviewOut } from "../lib/api";
import { SeverityBadge } from "./Badges";

type Decision = "approve" | "override" | "request_evidence";
const LEVELS = ["critical", "high", "medium", "low", "info"] as const;

interface Props {
  review: ReviewOut;
  onDecided: (result: DecisionOut) => void;
}

export default function ReviewForm({ review, onDecided }: Props) {
  const [decision, setDecision] = useState<Decision>("approve");
  const [reason, setReason] = useState("");
  const [level, setLevel] = useState<(typeof LEVELS)[number]>("high");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reasonRequired = decision !== "approve";
  const blocked = busy || (reasonRequired && reason.trim().length === 0);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await decideReview(review.id, {
        decision,
        reason: reason.trim() || null,
        new_level: decision === "override" ? level : null,
      });
      onDecided(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The decision was not recorded");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel review-form" onSubmit={(event) => void submit(event)}>
      <h2>Record a decision</h2>
      <fieldset>
        <legend>Decision</legend>
        <label className="radio">
          <input
            type="radio"
            name="decision"
            value="approve"
            checked={decision === "approve"}
            onChange={() => setDecision("approve")}
          />
          Approve the assessment as computed
        </label>
        <label className="radio">
          <input
            type="radio"
            name="decision"
            value="override"
            checked={decision === "override"}
            onChange={() => setDecision("override")}
          />
          Override the risk level
        </label>
        <label className="radio">
          <input
            type="radio"
            name="decision"
            value="request_evidence"
            checked={decision === "request_evidence"}
            onChange={() => setDecision("request_evidence")}
          />
          Request more evidence
        </label>
      </fieldset>

      {decision === "override" ? (
        <>
          <label htmlFor="new-level">New overall level</label>
          <select
            id="new-level"
            value={level}
            onChange={(event) => setLevel(event.target.value as (typeof LEVELS)[number])}
          >
            {LEVELS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <p className="muted">
            Currently assessed as <SeverityBadge value={review.level} />; the original stays readable
            in the audit trail.
          </p>
        </>
      ) : null}

      <label htmlFor="reason">
        Reason {reasonRequired ? <span aria-hidden="true">*</span> : "(optional)"}
      </label>
      <textarea
        id="reason"
        rows={4}
        required={reasonRequired}
        aria-required={reasonRequired}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder={
          reasonRequired
            ? "Why is this decision right? The note is stored with your account."
            : "Optional note for the record."
        }
      />

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}

      <button type="submit" className="button primary" disabled={blocked}>
        {busy ? "Recording…" : "Record decision"}
      </button>
      {blocked && reasonRequired && reason.trim().length === 0 ? (
        <p className="muted" role="status">
          A reason is required for this decision.
        </p>
      ) : null}
    </form>
  );
}
