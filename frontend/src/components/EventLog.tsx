import { useEffect, useRef, useState } from "react";

export interface LogLine {
  id: string;
  at: string;
  text: string;
}

/** A live feed. Auto-scroll is a toggle so a reader can stop the list moving. */
export default function EventLog({ lines }: { lines: LogLine[] }) {
  const [follow, setFollow] = useState(true);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (follow) endRef.current?.scrollIntoView({ block: "nearest" });
  }, [lines, follow]);

  return (
    <section className="panel" aria-label="Event log">
      <div className="panel-head">
        <h2>Event log</h2>
        <label className="inline-check">
          <input
            type="checkbox"
            checked={follow}
            onChange={(event) => setFollow(event.target.checked)}
          />
          Follow latest
        </label>
      </div>
      <ol className="event-log" tabIndex={0} aria-live="polite">
        {lines.length === 0 ? <li className="muted">Waiting for the first event…</li> : null}
        {lines.map((line) => (
          <li key={line.id}>
            <time dateTime={line.at} className="mono muted">
              {new Date(line.at).toLocaleTimeString()}
            </time>{" "}
            {line.text}
          </li>
        ))}
      </ol>
      <div ref={endRef} />
    </section>
  );
}
