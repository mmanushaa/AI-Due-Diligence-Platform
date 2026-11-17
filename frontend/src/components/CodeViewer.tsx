import type { CodeOut } from "../lib/api";

/** Read-only source view. Lines arrive already masked by the API; the client never
 *  receives a token, so there is nothing here for a copy button to leak. */
export default function CodeViewer({ code }: { code: CodeOut }) {
  async function copyPath() {
    try {
      await navigator.clipboard.writeText(code.file_path);
    } catch {
      // Clipboard permission can be refused; the path is visible either way.
    }
  }

  return (
    <section className="code-viewer" aria-label={`Code around ${code.file_path}`}>
      <div className="code-head">
        <code>{code.file_path}</code>
        <span className="muted">
          lines {code.line_start}–{code.line_end}
        </span>
        <button type="button" className="button ghost small" onClick={() => void copyPath()}>
          Copy path
        </button>
      </div>
      {code.masked ? (
        <p className="mask-note" role="status">
          Secret-shaped values are masked before they reach the browser.
        </p>
      ) : null}
      <pre className="code" tabIndex={0}>
        <code>
          {code.lines.map((line) => (
            <span
              key={line.number}
              className={`code-line${line.highlighted ? " highlighted" : ""}`}
            >
              <span className="code-number" aria-hidden="true">
                {line.number}
              </span>
              <span className="code-text">
                {line.text}
                {line.masked ? <span className="mask-tag"> masked</span> : null}
              </span>
            </span>
          ))}
        </code>
      </pre>
      {code.truncated ? (
        <p className="muted">This file is truncated in the viewer; the snapshot is unchanged.</p>
      ) : null}
    </section>
  );
}
