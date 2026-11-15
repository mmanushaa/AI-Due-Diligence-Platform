import { useEffect, useMemo, useRef, useState } from "react";
import { progressEventsUrl, type ProgressSnapshot, type StageState } from "../lib/api";

/** Display order and labels. The server sends the states; only the names live here. */
const STAGES: { key: string; label: string }[] = [
  { key: "intake", label: "Intake" },
  { key: "index", label: "Index" },
  { key: "scanners", label: "Scanners" },
  { key: "agents", label: "Agents" },
  { key: "validation", label: "Validation" },
  { key: "risk", label: "Risk" },
  { key: "review", label: "Human review" },
  { key: "report", label: "Report" },
];

const ICONS: Record<StageState, string> = {
  pending: "○",
  running: "◐",
  done: "●",
  failed: "✕",
  skipped: "–",
};

type Connection = "connecting" | "live" | "ended" | "error";

export default function StageTracker({
  auditId,
  onProgress,
}: {
  auditId: string;
  onProgress?: (snapshot: ProgressSnapshot) => void;
}) {
  const [snapshot, setSnapshot] = useState<ProgressSnapshot | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  // The elapsed time in the snapshot was true when it arrived; add the time since.
  const receivedAt = useRef<number>(Date.now());
  const onProgressRef = useRef(onProgress);
  onProgressRef.current = onProgress;

  useEffect(() => {
    const source = new EventSource(progressEventsUrl(auditId));

    const apply = (next: ProgressSnapshot) => {
      receivedAt.current = Date.now();
      setSnapshot(next);
      setConnection(next.final ? "ended" : "live");
      onProgressRef.current?.(next);
      if (next.final) source.close();
    };

    source.addEventListener("snapshot", (event) => apply(JSON.parse((event as MessageEvent).data)));
    source.addEventListener("progress", (event) => apply(JSON.parse((event as MessageEvent).data)));
    source.addEventListener("end", (event) => apply(JSON.parse((event as MessageEvent).data)));

    source.onerror = () => {
      // EventSource reconnects on its own; the server replays a snapshot on reconnect.
      setConnection((current) => (current === "ended" ? current : "error"));
      setError("live connection dropped; reconnecting…");
    };

    return () => source.close();
  }, [auditId]);

  // The counter ticks between events instead of waiting for the next stage.
  useEffect(() => {
    if (!snapshot || snapshot.final) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [snapshot]);

  const elapsedMs = useMemo(() => {
    if (!snapshot) return 0;
    const drift = snapshot.final ? 0 : Math.max(0, now - receivedAt.current);
    return snapshot.elapsed_ms + drift;
  }, [snapshot, now]);

  const costUsd = snapshot?.cost_usd ?? 0;
  const stages = snapshot?.stages ?? {};

  return (
    <section className="tracker" aria-live="polite">
      <header className="tracker-head">
        <div>
          <h2>Live progress</h2>
          <p className="muted">
            {snapshot ? snapshot.status.replace("_", " ") : "connecting"} ·{" "}
            <span className={`conn conn-${connection}`}>{connection}</span>
          </p>
        </div>
        <dl className="tracker-metrics">
          <div>
            <dt>Elapsed</dt>
            <dd>{(elapsedMs / 1000).toFixed(1)}s</dd>
          </div>
          <div>
            <dt>Cost so far</dt>
            <dd className="cost">${costUsd.toFixed(4)}</dd>
          </div>
        </dl>
      </header>

      {error && <p className="field-error">{error}</p>}

      <ol className="stage-list">
        {STAGES.map(({ key, label }) => {
          const state: StageState = (stages[key] as StageState) ?? "pending";
          const active = snapshot?.current_stage === key;
          return (
            <li key={key} className={`stage-row stage-${state}${active ? " active" : ""}`}>
              <span className="stage-icon" aria-hidden="true">
                {ICONS[state]}
              </span>
              <span className="stage-label">{label}</span>
              <span className={`stage-state state-${state}`}>{state}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
